"""Opt-in isolated container regression test: PORTAL_TEST_IMAGE=<built image>."""
import os
import subprocess
import unittest


@unittest.skipUnless(os.environ.get('PORTAL_TEST_IMAGE'), 'Set PORTAL_TEST_IMAGE to test a built image')
class ContainerRuntimeTest(unittest.TestCase):
    def test_nonroot_ansible_environment_and_collections(self):
        # No host data, libvirt socket, network or persistent volumes are exposed.
        subprocess.run([
            'docker', 'run', '--rm', '--network', 'none', '--tmpfs', '/data',
            '-e', 'HOME=/root', os.environ['PORTAL_TEST_IMAGE'], 'sh', '-ec',
            '''
test "$(id -u)" = 1000
test "$HOME" = /home/portal
test "$USER" = portal
test "$LOGNAME" = portal
ansible --version
test -w "$HOME/.ansible/tmp"
ansible-doc -t module community.vmware.vmware_guest --json >/dev/null
ansible localhost -i localhost, -c local -m ansible.builtin.command -a /usr/bin/id
'''], check=True, timeout=60)


if __name__ == '__main__':
    unittest.main()
