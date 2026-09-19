import unittest
import http.client
import json
import subprocess
from pathlib import Path
import tempfile
import threading
import time
from unittest.mock import patch

import app
from netbox_client import NetBoxClient, NetBoxError


class NetBoxTest(unittest.TestCase):
    def setUp(self):
        self.request = dict(id='netbox-test', hostname='testvm', fqdn='testvm.example.test',
                            status='completed', resources=dict(vcpu=2, memory_gb=4, disk_gb=40),
                            additional_disks=[dict(size_gb=20)], events=[])
        self.client = NetBoxClient('http://localhost:9080', 'secret-test-token', 1)

    @staticmethod
    def listing(*items):
        return {'count': len(items), 'results': list(items)}

    def test_create_vm_and_resource_units(self):
        with patch.object(self.client, 'api', side_effect=[self.listing(), {'id': 5}]) as api:
            result = self.client.register(self.request, self.request['additional_disks'])
        payload = api.call_args_list[1].args[2]
        self.assertEqual((payload['vcpus'], payload['memory'], payload['disk']), (2, 4096, 61440))
        self.assertEqual(result['vm_id'], 5)
        self.assertNotIn('secret-test-token', str(result))

    def test_retry_updates_owned_vm_instead_of_creating_duplicate(self):
        existing = {'id': 5, 'comments': 'VM Foundry request: netbox-test'}
        with patch.object(self.client, 'api', side_effect=[self.listing(existing), {'id': 5}]) as api:
            self.client.register(self.request, [])
        self.assertEqual(api.call_args_list[1].args[:2], ('PATCH', 'virtualization/virtual-machines/5/'))

    def test_name_collision_does_not_overwrite(self):
        with patch.object(self.client, 'api', return_value=self.listing({'id': 5, 'comments': 'someone else'})) as api:
            with self.assertRaises(NetBoxError):
                self.client.register(self.request, [])
            api.assert_called_once()

    def test_ip_registered_and_assigned_as_primary(self):
        self.request['ip_address'] = '192.0.2.20'
        with patch.object(self.client, 'api', side_effect=[self.listing(), {'id': 5},
                self.listing(), {'id': 6}, self.listing(), {'id': 7}, {'id': 5}]) as api:
            result = self.client.register(self.request, [])
        self.assertEqual(api.call_args_list[5].args[2]['address'], '192.0.2.20/32')
        self.assertEqual(api.call_args_list[6].args[2], {'primary_ip4': 7})
        self.assertEqual(result['ip_address'], '192.0.2.20')

    def test_ip_owned_elsewhere_is_not_reassigned(self):
        self.request['ip_address'] = '192.0.2.20'
        with patch.object(self.client, 'api', side_effect=[self.listing(), {'id': 5},
                self.listing({'id': 6}), self.listing({'id': 7, 'assigned_object_type': 'dcim.interface',
                                                   'assigned_object_id': 6})]) as api:
            with self.assertRaisesRegex(NetBoxError, 'refusing to reassign'):
                self.client.register(self.request, [])
        self.assertEqual(len(api.call_args_list), 4)

    def test_disabled_does_not_contact_netbox(self):
        with patch.object(app, 'NETBOX_ENABLED', False), patch.object(app, 'NetBoxClient') as client:
            self.assertFalse(app.register_netbox(self.request))
            client.assert_not_called()

    def test_inventory_error_preserves_completed_vm_and_retry_reuses_event(self):
        with patch.object(app, 'NETBOX_ENABLED', True), patch.object(app, 'NetBoxClient') as client:
            client.return_value.register.side_effect = NetBoxError('NetBox unavailable')
            self.assertFalse(app.register_netbox(self.request))
            self.assertEqual(self.request['status'], 'completed')
            self.assertEqual(self.request['netbox']['status'], 'error')
            client.return_value.register.side_effect = None
            client.return_value.register.return_value = {'vm_id': 5}
            self.assertTrue(app.register_netbox(self.request))
        self.assertEqual(len(self.request['events']), 1)
        self.assertEqual(self.request['events'][0]['state'], 'done')

    def test_http_error_does_not_expose_response_body_or_token(self):
        import io
        from urllib.error import HTTPError
        with patch.object(self.client.opener, 'open', side_effect=HTTPError(
                'http://localhost', 403, 'secret-test-token', {}, io.BytesIO(b'sensitive body'))):
            with self.assertRaises(NetBoxError) as error:
                self.client.api('GET', 'virtualization/virtual-machines/')
        self.assertNotIn('secret-test-token', str(error.exception))
        self.assertNotIn('sensitive body', str(error.exception))


class NetBoxRetryAPITest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        for key, value in [('DATA_FILE', Path(directory.name) / 'portal.db'),
                           ('NETBOX_ENABLED', True), ('SESSIONS', {
                               'owner-token': dict(username='owner', role='user', expires=time.time()+60),
                               'other-token': dict(username='other', role='user', expires=time.time()+60)})]:
            patcher = patch.object(app, key, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.record = dict(id='netbox-api-test', hostname='test', requested_by='owner', created_at=app.now(),
                           status='completed', resources=dict(vcpu=2, memory_gb=4, disk_gb=40), events=[])
        app.save_requests([self.record])
        self.server = app.ThreadingHTTPServer(('127.0.0.1', 0), app.PortalHandler)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        def close():
            self.server.shutdown()
            self.server.server_close()
            thread.join()
        self.addCleanup(close)

    def post(self, token, action='register-netbox', payload=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
        connection.request('POST', f'/api/requests/netbox-api-test/{action}', body=json.dumps(payload or {}),
                           headers={'Content-Type': 'application/json', 'Cookie': f'vmportal_session={token}'})
        response = connection.getresponse()
        result = response.status, json.loads(response.read())
        connection.close()
        return result

    def test_other_user_cannot_trigger_registration(self):
        with patch.object(app, 'NetBoxClient') as client:
            self.assertEqual(self.post('other-token')[0], 403)
            client.assert_not_called()

    def test_owner_retry_updates_saved_registration(self):
        with patch.object(app, 'NetBoxClient') as client:
            client.return_value.register.return_value = {'vm_id': 42}
            self.assertEqual(self.post('owner-token')[0], 200)
        self.assertEqual(app.load_requests()[0]['netbox']['vm_id'], 42)

    def test_api_failure_keeps_completed_status(self):
        with patch.object(app, 'NetBoxClient') as client:
            client.return_value.register.side_effect = NetBoxError('NetBox unavailable')
            self.assertEqual(self.post('owner-token')[0], 502)
        self.assertEqual(app.load_requests()[0]['status'], 'completed')
        self.assertEqual(app.load_requests()[0]['netbox']['status'], 'error')

    def test_failed_vm_cannot_register(self):
        self.record['status'] = 'failed'
        app.save_requests([self.record])
        with patch.object(app, 'NetBoxClient') as client:
            self.assertEqual(self.post('owner-token')[0], 409)
            client.assert_not_called()

    def run_resize(self, action='resize', inventory_error=False, ansible_error=False):
        resources = dict(vcpu=4, memory_gb=8, disk_gb=60)
        disks = [dict(size_gb=30, mountpoint='/data', target='vdb', storage_file='test-data-1.qcow2')]
        self.record['additional_disks'] = [dict(disks[0], size_gb=20)]
        if action == 'approve':
            self.record['status'] = 'resize_awaiting_approval'
            self.record['pending_resize'] = dict(resources=resources, additional_disks=disks, requested_by='owner')
            app.SESSIONS['owner-token']['role'] = 'admin'
        elif action == 'retry-resize':
            self.record['last_failed_resize'] = dict(resources=resources, additional_disks=disks)
        app.save_requests([self.record])
        with patch.object(app, 'PROVISIONING_BACKEND', 'local_qemu'), \
             patch.object(app, 'local_qemu_domain_exists', return_value=True), \
             patch.object(app, 'preflight_local_resize', return_value=[]), \
             patch.object(app, 'create_resize_snapshot', return_value=({'id': 'test-snapshot'}, '')), \
             patch.object(app, 'send_notification'), \
             patch.object(app.subprocess, 'run', return_value=subprocess.CompletedProcess([], int(ansible_error), '', '')), \
             patch.object(app, 'NetBoxClient') as client:
            if inventory_error:
                client.return_value.register.side_effect = NetBoxError('NetBox unavailable')
            else:
                client.return_value.register.return_value = {'vm_id': 42}
            status, result = self.post('owner-token', action, dict(resources, additional_disks=disks))
            if ansible_error:
                self.assertEqual(status, 409)
                client.assert_not_called()
                return
            self.assertEqual(status, 200, result)
            request, actual_disks = client.return_value.register.call_args.args
            self.assertEqual(request['resources'], resources)
            self.assertEqual(actual_disks, disks)
            saved = app.load_requests()[0]
            self.assertEqual(saved['resources'], resources)
            self.assertEqual(saved['additional_disks'], disks)
            self.assertEqual(saved['status'], 'completed')
            self.assertEqual(saved['netbox']['status'], 'error' if inventory_error else 'registered')
            self.assertNotIn('last_failed_resize', saved)

    def test_direct_resize_syncs_new_values(self):
        self.run_resize()

    def test_approved_resize_syncs_new_values(self):
        self.run_resize('approve')

    def test_retried_resize_syncs_new_values(self):
        self.run_resize('retry-resize')

    def test_successful_resize_survives_netbox_failure(self):
        self.run_resize(inventory_error=True)

    def test_failed_resize_does_not_sync_requested_values(self):
        self.run_resize(ansible_error=True)


if __name__ == '__main__':
    unittest.main()
