"""Deployment generation tests: isolated directories, no host reconfiguration."""
import importlib.util
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parents[2]


def module(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / f'scripts/{name}.py')
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


runtime = module('configure_runtime')
images = module('prepare_image')


class InstallerTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'portal').mkdir()
        shutil.copy(ROOT / 'portal/config.yml', self.root / 'portal/config.yml')
        self.data = self.root / 'data'
        self.storage = self.data / 'instances'
        self.storage.mkdir(parents=True)
        self.cloud = self.data / 'cloud-init/user-data.yml'
        self.cloud.parent.mkdir()
        self.cloud.write_text('#cloud-config\nusers: []\n')
        self.sock = self.root / 'libvirt-sock'
        self.socket = socket.socket(socket.AF_UNIX)
        self.socket.bind(str(self.sock))
        self.addCleanup(self.socket.close)
        self.image = self.data / 'templates/ubuntu-24.04-lvm/ubuntu-24.04-lvm.qcow2'
        self.image.parent.mkdir(parents=True)
        self.image.write_bytes(b'example-existing-image')

    def configure(self):
        runtime.configure(self.root, 'local_qemu', self.data, self.storage, self.cloud, self.sock)

    def config(self):
        return yaml.safe_load((self.root / '.runtime/config.yml').read_text())

    def test_fresh_install_and_identical_host_paths(self):
        self.configure()
        qemu = self.config()['provisioning']['local_qemu']
        self.assertEqual(qemu['storage_path'], str(self.storage))
        self.assertEqual(list(qemu['images'].values()), [str(self.image)])
        override = yaml.safe_load((self.root / '.runtime/docker-compose.local-qemu.yml').read_text())
        for mount in override['services']['portal']['volumes'][1:]:
            self.assertEqual(mount['source'], mount['target'])
            self.assertFalse(mount['bind']['create_host_path'])

    def test_rerun_preserves_custom_settings_and_images(self):
        self.configure()
        config = self.config()
        config['authentication']['ldap_url'] = 'ldaps://directory.example.com'
        config['notifications']['to'] = 'operator@example.com'
        other = self.root / 'other.qcow2'
        other.write_bytes(b'custom')
        config['provisioning']['local_qemu']['images']['Custom Linux'] = str(other)
        runtime.write_yaml(self.root / '.runtime/config.yml', config)
        self.configure()
        self.assertEqual(self.config(), config)
        self.assertTrue((self.root / '.runtime/config.previous.yml').exists())
        self.assertEqual(self.image.read_bytes(), b'example-existing-image')
        runtime.configure(self.root, 'vcsim')
        self.assertEqual(self.config()['provisioning']['backend'], 'vcsim')
        self.assertEqual(self.config()['notifications']['to'], 'operator@example.com')

    def test_no_images_does_not_replace_runtime(self):
        self.configure()
        before = (self.root / '.runtime/config.yml').read_bytes()
        self.image.unlink()
        with self.assertRaisesRegex(ValueError, 'No usable images'):
            self.configure()
        self.assertEqual((self.root / '.runtime/config.yml').read_bytes(), before)

    def test_existing_image_never_built_again(self):
        with patch.object(images.subprocess, 'run') as run:
            images.build(self.root, self.data, '24.04')
        run.assert_called_once_with(['qemu-img', 'info', str(self.image)], check=True)
        self.assertEqual(self.image.read_bytes(), b'example-existing-image')

    def test_partial_output_is_not_overwritten(self):
        self.image.unlink()
        with self.assertRaisesRegex(ValueError, 'Refusing to overwrite'):
            images.build(self.root, self.data, '24.04')

    def test_failed_build_keeps_existing_other_version(self):
        with patch.object(images.os, 'access', return_value=True), \
             patch.object(images, 'packer_binary', return_value='packer'), \
             patch.object(images.subprocess, 'run', side_effect=subprocess.CalledProcessError(1, 'packer')):
            with self.assertRaises(subprocess.CalledProcessError):
                images.build(self.root, self.data, '22.04')
        self.assertEqual(self.image.read_bytes(), b'example-existing-image')
        self.assertFalse((self.data / 'templates/ubuntu-22.04-lvm').exists())

    def test_successful_build_installs_only_after_check(self):
        commands = []
        def run(command, **kwargs):
            commands.append(command)
            if command[1] == 'build':
                output = Path(command[3].split('=', 1)[1])
                output.mkdir(parents=True)
                (output / 'ubuntu-22.04-lvm.qcow2').write_bytes(b'new image')
        with patch.object(images.os, 'access', return_value=True), \
             patch.object(images, 'packer_binary', return_value='packer'), \
             patch.object(images.subprocess, 'run', side_effect=run):
            images.build(self.root, self.data, '22.04')
        image = self.data / 'templates/ubuntu-22.04-lvm/ubuntu-22.04-lvm.qcow2'
        self.assertEqual(image.read_bytes(), b'new image')
        self.assertEqual(image.stat().st_mode & 0o777, 0o644)
        self.assertEqual(commands[-1][:2], ['qemu-img', 'check'])
        self.assertEqual(self.image.read_bytes(), b'example-existing-image')

    def test_saved_legacy_paths_are_reused(self):
        shutil.copy(ROOT / 'install.sh', self.root / 'install.sh')
        # Legacy storage and cloud-init names differ from new defaults.
        legacy = self.data / 'vm-foundry'
        legacy.mkdir()
        cloud = self.data / 'cloud_init.cfg.orig'
        cloud.write_text('legacy template')
        (self.root / '.env').write_text(
            f'KVM_READONLY_PATH={self.data}\nKVM_STORAGE_PATH={legacy}\nLIBVIRT_SOCKET_PATH={self.sock}\n')
        command = '''source ./install.sh
ensure_local_qemu() { :; }
run_as_root() { if [[ "$1" == install ]]; then "$@"; fi; }
configure_local_qemu_paths
'''
        subprocess.run(['bash', '-c', command], cwd=self.root, input='\n\n', text=True, check=True,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        env = (self.root / '.env').read_text()
        self.assertIn(f'KVM_STORAGE_PATH={legacy}', env)
        self.assertIn(f'CLOUD_INIT_TEMPLATE={cloud}', env)
        self.assertEqual(cloud.read_text(), 'legacy template')

    @unittest.skipUnless(shutil.which('docker'), 'Docker CLI required for Compose validation')
    def test_generated_compose_accepts_new_and_simulator_configs(self):
        self.configure()
        shutil.copy(ROOT / 'docker-compose.yml', self.root / 'docker-compose.yml')
        (self.root / '.env').write_text(
            "LOCAL_ADMIN_USERNAME=admin\nLOCAL_ADMIN_PASSWORD_HASH='test-hash'\n"
            "PORTAL_CONFIG_PATH=./.runtime/config.yml\n")
        for files in [('docker-compose.yml', '.runtime/docker-compose.local-qemu.yml'),
                      ('docker-compose.yml',)]:
            command = ['docker', 'compose']
            for file in files:
                command += ['-f', str(self.root / file)]
            subprocess.run(command + ['config', '--quiet'], cwd=self.root, check=True,
                           env={k: v for k, v in os.environ.items() if not k.startswith('COMPOSE_')})


if __name__ == '__main__':
    unittest.main()
