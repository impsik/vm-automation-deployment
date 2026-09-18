import http.client
import json
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import app


class VmDeletionIntegrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        backend = patch.object(app, 'PROVISIONING_BACKEND', 'local_qemu')
        backend.start()
        cls.addClassCleanup(backend.stop)
        cls.test_directory = Path(tempfile.mkdtemp(prefix="vm-foundry-delete-tests-"))
        app.DATA_FILE = cls.test_directory / "portal.db"
        cls.original_storage_path = app.LOCAL_QEMU.get("storage_path")
        app.LOCAL_QEMU["storage_path"] = str(cls.test_directory / "storage")
        Path(app.LOCAL_QEMU["storage_path"]).mkdir()
        app.SESSIONS.clear()
        expires = time.time() + 3600
        app.SESSIONS["owner-token"] = {
            "username": "owner", "role": "user", "auth_source": "test", "expires": expires,
        }
        app.SESSIONS["other-token"] = {
            "username": "other", "role": "user", "auth_source": "test", "expires": expires,
        }
        app.SESSIONS["admin-token"] = {
            "username": "admin", "role": "admin", "auth_source": "test", "expires": expires,
        }
        cls.server = app.ThreadingHTTPServer(("127.0.0.1", 0), app.PortalHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.port = cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)
        app.LOCAL_QEMU["storage_path"] = cls.original_storage_path

    def setUp(self):
        app.VM_DELETE_TICKETS.clear()
        with app.database() as connection:
            connection.execute("DELETE FROM requests")
        self.request_id = f"delete-{self._testMethodName[-16:].replace('_', '-')}"
        self.hostname = self.request_id
        self.request_record = {
            "id": self.request_id,
            "created_at": app.now(),
            "status": "completed",
            "hostname": self.hostname,
            "requested_by": "owner",
            "resources": {"vcpu": 2, "memory_gb": 4, "disk_gb": 40},
            "events": [app.event("VM created"), app.event("VM ready")],
        }
        app.save_requests([self.request_record])
        self.disk = Path(app.LOCAL_QEMU["storage_path"]) / f"{self.hostname}.qcow2"
        self.disk.write_bytes(b"test")

    def request(self, method, path, token, payload=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        body = json.dumps(payload).encode() if payload is not None else None
        headers = {"Cookie": f"vmportal_session={token}"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        result = json.loads(response.read() or b"{}")
        connection.close()
        return response.status, result

    @patch.object(app, "deletion_domain_exists", return_value=True)
    def test_owner_can_inspect_exact_scope(self, _exists):
        status, plan = self.request(
            "GET", f"/api/requests/{self.request_id}/delete-plan", "owner-token"
        )
        self.assertEqual(status, 200)
        self.assertIn("confirmation_token", plan)
        self.assertNotIn("delete_targets", plan)
        self.assertNotIn("database_records", plan)
        self.assertNotIn(str(self.disk.parent), json.dumps(plan))

    @patch.object(app, "deletion_domain_exists", return_value=True)
    def test_other_user_cannot_inspect_scope(self, _exists):
        status, _ = self.request(
            "GET", f"/api/requests/{self.request_id}/delete-plan", "other-token"
        )
        self.assertEqual(status, 403)

    @patch.object(app, "deletion_domain_exists", return_value=False)
    def test_changed_scope_is_rejected(self, _exists):
        status, _ = self.request(
            "DELETE", f"/api/requests/{self.request_id}", "owner-token",
            {"confirm_hostname": self.hostname, "confirmation_token": "stale"},
        )
        self.assertEqual(status, 422)
        self.assertTrue(self.disk.exists())
        self.assertTrue(any(item["id"] == self.request_id for item in app.load_requests()))

    @patch.object(app, "deletion_domain_exists", return_value=False)
    def test_admin_deletes_files_request_and_cascaded_events(self, _exists):
        status, plan = self.request(
            "GET", f"/api/requests/{self.request_id}/delete-plan", "admin-token"
        )
        self.assertEqual(status, 200)
        status, result = self.request(
            "DELETE", f"/api/requests/{self.request_id}", "admin-token",
            {"confirm_hostname": self.hostname, "confirmation_token": plan["confirmation_token"]},
        )
        self.assertEqual(status, 200)
        self.assertTrue(result["deleted"])
        self.assertFalse(self.disk.exists())
        with app.database() as connection:
            request_count = connection.execute(
                "SELECT COUNT(*) FROM requests WHERE id = ?", (self.request_id,)
            ).fetchone()[0]
            event_count = connection.execute(
                "SELECT COUNT(*) FROM events WHERE request_id = ?", (self.request_id,)
            ).fetchone()[0]
        self.assertEqual((request_count, event_count), (0, 0))

    def mark_failed(self):
        self.request_record['status'] = 'failed'
        app.save_requests([self.request_record])

    def plan(self):
        return self.request('GET', f'/api/requests/{self.request_id}/delete-plan', 'owner-token')

    def delete(self, plan, **extra):
        return self.request('DELETE', f'/api/requests/{self.request_id}', 'owner-token',
                            dict(confirm_hostname=self.hostname,
                                 confirmation_token=plan['confirmation_token'], **extra))

    @patch.object(app, 'deletion_domain_exists', return_value=False)
    def test_failed_missing_vm_without_files(self, _exists):
        self.mark_failed()
        self.disk.unlink()
        status, plan = self.plan()
        self.assertEqual(status, 200)
        self.assertTrue(plan['failed_request'])
        self.assertEqual(plan['file_count'], 0)
        with patch.object(app.subprocess, 'run') as run:
            self.assertEqual(self.delete(plan)[0], 200)
            run.assert_not_called()
        self.assertEqual(app.load_requests(), [])

    @patch.object(app, 'deletion_domain_exists', return_value=False)
    def test_failed_leftovers_require_separate_confirmation(self, _exists):
        self.mark_failed()
        status, plan = self.plan()
        self.assertEqual(status, 200)
        self.assertEqual(plan['file_names'], [self.disk.name])
        self.assertEqual(self.delete(plan)[0], 409)
        self.assertTrue(self.disk.exists())
        _, plan = self.plan()
        self.assertEqual(self.delete(plan, confirm_files=True)[0], 200)
        self.assertFalse(self.disk.exists())

    @patch.object(app, 'deletion_domain_exists', return_value=True)
    def test_failed_existing_domain_is_not_destroyed(self, _exists):
        self.mark_failed()
        with patch.object(app.subprocess, 'run') as run:
            self.assertEqual(self.plan()[0], 409)
            run.assert_not_called()
        self.assertTrue(self.disk.exists())

    def test_libvirt_errors_are_not_treated_as_absence(self):
        self.mark_failed()
        for result in (subprocess.CompletedProcess([], 1, '', 'permission denied'),
                       subprocess.CompletedProcess([], 1, '', 'connection refused')):
            with patch.object(app.subprocess, 'run', return_value=result):
                self.assertEqual(self.plan()[0], 409)
        with patch.object(app.subprocess, 'run', side_effect=subprocess.TimeoutExpired('virsh', 15)):
            self.assertEqual(self.plan()[0], 409)
        self.assertTrue(self.disk.exists())
        self.assertEqual(len(app.load_requests()), 1)

    def test_successful_domain_listing_checks_exact_name(self):
        with patch.object(app.subprocess, 'run', return_value=subprocess.CompletedProcess(
                [], 0, 'other\n' + self.hostname + '-suffix\n', '')):
            self.assertFalse(app.deletion_domain_exists(self.hostname))
        with patch.object(app.subprocess, 'run', return_value=subprocess.CompletedProcess(
                [], 0, self.hostname + '\n', '')):
            self.assertTrue(app.deletion_domain_exists(self.hostname))

    @patch.object(app, 'deletion_domain_exists', return_value=False)
    def test_failed_scope_change_is_rejected(self, _exists):
        self.mark_failed()
        _, plan = self.plan()
        seed = self.disk.with_name(self.hostname + '-seed.img')
        seed.write_bytes(b'seed')
        self.addCleanup(seed.unlink)
        self.assertEqual(self.delete(plan, confirm_files=True)[0], 409)
        self.assertTrue(self.disk.exists())

    def test_failed_vm_appearing_after_confirmation_is_not_deleted(self):
        self.mark_failed()
        with patch.object(app, 'deletion_domain_exists', return_value=False):
            _, plan = self.plan()
        with patch.object(app, 'deletion_domain_exists', return_value=True), \
             patch.object(app.subprocess, 'run') as run:
            self.assertEqual(self.delete(plan, confirm_files=True)[0], 409)
            run.assert_not_called()
        self.assertTrue(self.disk.exists())

    @patch.object(app, 'deletion_domain_exists', return_value=False)
    def test_failed_request_owner_permissions(self, _exists):
        self.mark_failed()
        status, _ = self.request('GET', f'/api/requests/{self.request_id}/delete-plan', 'other-token')
        self.assertEqual(status, 403)
        self.assertTrue(self.disk.exists())

    @patch.object(app, 'deletion_domain_exists', return_value=False)
    def test_duplicate_hostname_blocks_failed_cleanup(self, _exists):
        self.mark_failed()
        other = dict(self.request_record, id='other-request', status='completed')
        app.save_requests([self.request_record, other])
        self.assertEqual(self.plan()[0], 409)
        self.assertTrue(self.disk.exists())


if __name__ == "__main__":
    unittest.main()
