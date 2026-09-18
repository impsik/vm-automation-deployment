"""Generate deployment YAML without changing the tracked example or VM files."""
import argparse
import os
from pathlib import Path
import shutil
import tempfile

import yaml


def write_yaml(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as out:
        yaml.safe_dump(value, out, sort_keys=False)
        temporary = Path(out.name)
    temporary.chmod(0o644)
    os.replace(temporary, path)


def configure(root, backend, data=None, storage=None, cloud_init=None, socket=None):
    root = Path(root)
    runtime = root / '.runtime'
    target = runtime / 'config.yml'
    source = target if target.exists() else root / 'portal/config.yml'
    config = yaml.safe_load(source.read_text())
    provisioning = config.setdefault('provisioning', {})
    provisioning['backend'] = backend
    if backend == 'local_qemu':
        data, storage, cloud_init, socket = map(Path, (data, storage, cloud_init, socket))
        local = provisioning.setdefault('local_qemu', {})
        images = {name: path for name, path in local.get('images', {}).items()
                  if Path(path).is_file()}
        for version in ('22.04', '24.04'):
            image = data / f'templates/ubuntu-{version}-lvm/ubuntu-{version}-lvm.qcow2'
            if image.is_file():
                images[f'Ubuntu {version} LTS (LVM)'] = str(image)
        if not images:
            raise ValueError('No usable images found; prepare a template first')
        local.update(storage_path=str(storage), cloud_init_template=str(cloud_init), images=images)
        local.setdefault('uri', 'qemu:///system')
        local.setdefault('network', 'default')
        local.setdefault('ssh_user', 'ubuntu')
        mounts = [dict(type='bind', source=str(socket), target='/var/run/libvirt/libvirt-sock',
                       bind=dict(create_host_path=False)),
                  dict(type='bind', source=str(data), target=str(data), read_only=True,
                       bind=dict(create_host_path=False)),
                  dict(type='bind', source=str(storage), target=str(storage),
                       bind=dict(create_host_path=False))]
        # Preserve custom images/templates located outside the data directory.
        for path in dict.fromkeys([str(cloud_init), *images.values()]):
            if not Path(path).is_relative_to(data):
                mounts.append(dict(type='bind', source=path, target=path, read_only=True,
                                   bind=dict(create_host_path=False)))
        write_yaml(runtime / 'docker-compose.local-qemu.yml', dict(services=dict(portal=dict(
            group_add=[str(socket.stat().st_gid)], volumes=mounts))))
    if target.exists():
        shutil.copy2(target, runtime / 'config.previous.yml')
    write_yaml(target, config)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('root')
    parser.add_argument('backend', choices=['vcsim', 'local_qemu'])
    for name in ('data', 'storage', 'cloud-init', 'socket'):
        parser.add_argument('--' + name)
    args = parser.parse_args()
    configure(args.root, args.backend, args.data, args.storage, args.cloud_init, args.socket)
