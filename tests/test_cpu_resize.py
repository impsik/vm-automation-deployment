"""Run the actual resize playbook against a stateful, isolated virsh double."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import app


FAKE_VIRSH = r'''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
path = Path(os.environ['CPU_TEST_STATE'])
state = json.loads(path.read_text())
args = sys.argv[3:]
state.setdefault('calls', []).append(args)
command = args[0]
output = ''
rc = 0
if command == 'dominfo':
    output = 'State: running\nCPU(s): %s\nUsed memory: 4194304 KiB' % state['current']
elif command == 'domblkinfo':
    output = 'Capacity: 42949672960'
elif command == 'vcpucount':
    output = '32'
elif command == 'dumpxml':
    output = "<domain><maxMemory unit='KiB'>33554432</maxMemory></domain>"
elif command == 'qemu-agent-command':
    rpc = json.loads(args[2])['execute']
    if state.get('agent_down'):
        output, rc = 'Guest agent unavailable', 1
    elif rpc == 'guest-ping':
        output = '{"return": {}}'
    elif rpc == 'guest-get-vcpus':
        output = json.dumps({'return': [
            {'logical-id': i, 'online': i < state['online'], 'can-offline': i > 0}
            for i in range(state['present'])]})
        if state.get('pending'):
            state['present'] = state.pop('pending')
    else:
        raise AssertionError(rpc)
elif command == 'setvcpus':
    count = int(args[2])
    if '--guest' in args:
        if state.get('activation_fails'):
            output, rc = 'Guest CPU activation rejected', 1
        elif not state.get('activation_noop'):
            state['online'] = count
    else:
        assert '--live' in args and '--config' in args
        state['current'] = count
        if state.get('delayed'):
            state['pending'] = count
        else:
            state['present'] = count
else:
    raise AssertionError(args)
path.write_text(json.dumps(state))
print(output)
sys.exit(rc)
'''


@unittest.skipUnless(shutil.which('ansible-playbook'), 'ansible-playbook required')
class CpuResizePlaybookTest(unittest.TestCase):
    def run_resize(self, **overrides):
        with tempfile.TemporaryDirectory(prefix='cpu-resize-test-') as directory:
            root = Path(directory)
            state = {'current': 2, 'present': 2, 'online': 2, **overrides}
            state_file = root / 'state.json'
            state_file.write_text(json.dumps(state))
            virsh = root / 'virsh'
            virsh.write_text(FAKE_VIRSH)
            virsh.chmod(0o755)
            variables = {
                'vm_name': 'test-cpu', 'vm_vcpu': 4, 'vm_memory_mb': 4096,
                'vm_disk_gb': 40, 'resize_guest_disk': False, 'data_disks': [],
                'storage_path': directory,
            }
            result = subprocess.run(
                ['ansible-playbook', '-i', 'localhost,',
                 str(Path(__file__).resolve().parents[1] / 'ansible/resize-local-qemu.yml'),
                 '-e', json.dumps(variables)],
                env={**os.environ, 'PATH': directory + os.pathsep + os.environ['PATH'],
                     'CPU_TEST_STATE': str(state_file), 'ANSIBLE_NOCOLOR': '1'},
                capture_output=True, text=True, timeout=120,
            )
            return result, json.loads(state_file.read_text())

    def test_cpu_only_hot_add_waits_for_guest_then_activates(self):
        result, state = self.run_resize(delayed=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((state['current'], state['online']), (4, 4))
        self.assertEqual(len([c for c in state['calls'] if c[0] == 'setvcpus']), 2)

    def test_retry_activates_already_attached_offline_cpus(self):
        result, state = self.run_resize(current=4, present=4)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(state['online'], 4)
        changes = [c for c in state['calls'] if c[0] == 'setvcpus']
        self.assertEqual(changes, [['setvcpus', 'test-cpu', '4', '--guest']])

    def test_already_online_cpus_are_not_changed(self):
        result, state = self.run_resize(current=4, present=4, online=4)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(any(c[0] == 'setvcpus' for c in state['calls']))

    def test_unavailable_guest_agent_prevents_hot_add(self):
        result, state = self.run_resize(agent_down=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(state['current'], 2)
        self.assertFalse(any(c[0] == 'setvcpus' for c in state['calls']))

    def test_activation_failure_is_not_success(self):
        result, state = self.run_resize(activation_fails=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(state['online'], 2)
        self.assertIn('Guest CPU activation rejected', result.stdout)

    def test_successful_command_with_cpus_still_offline_is_failure(self):
        result, state = self.run_resize(activation_noop=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(state['online'], 2)
        self.assertIn('Verify the requested CPUs are online', result.stdout)


class CpuResizeResultTest(unittest.TestCase):
    @patch.object(app, 'send_notification')
    @patch.object(app.subprocess, 'run')
    def test_failed_guest_verification_keeps_previous_resources(self, run, notify):
        request = {
            'hostname': 'test-cpu', 'resources': {'vcpu': 2, 'memory_gb': 4, 'disk_gb': 40},
            'additional_disks': [], 'events': [],
        }
        run.return_value = subprocess.CompletedProcess([], 2, 'Guest CPUs still offline', '')
        success, detail = app.resize_local_qemu(
            request, {'vcpu': 4, 'memory_gb': 4, 'disk_gb': 40}, [], 'test',
            create_snapshot=False,
        )
        self.assertFalse(success)
        self.assertIn('offline', detail)
        self.assertEqual(request['resources']['vcpu'], 2)
        self.assertEqual(request['last_failed_resize']['resources']['vcpu'], 4)
        self.assertEqual(notify.call_args.args[0], 'failed')
