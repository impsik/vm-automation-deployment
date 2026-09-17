import http.client
import json
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

    @patch.object(app, "local_qemu_domain_exists", return_value=True)
    def test_owner_can_inspect_exact_scope(self, _exists):
        status, plan = self.request(
            "GET", f"/api/requests/{self.request_id}/delete-plan", "owner-token"
        )
        self.assertEqual(status, 200)
        self.assertIn("confirmation_token", plan)
        self.assertNotIn("delete_targets", plan)
        self.assertNotIn("database_records", plan)
        self.assertNotIn(str(self.disk.parent), json.dumps(plan))

    @patch.object(app, "local_qemu_domain_exists", return_value=True)
    def test_other_user_cannot_inspect_scope(self, _exists):
        status, _ = self.request(
            "GET", f"/api/requests/{self.request_id}/delete-plan", "other-token"
        )
        self.assertEqual(status, 403)

    @patch.object(app, "local_qemu_domain_exists", return_value=False)
    def test_changed_scope_is_rejected(self, _exists):
        status, _ = self.request(
            "DELETE", f"/api/requests/{self.request_id}", "owner-token",
            {"confirm_hostname": self.hostname, "confirmation_token": "stale"},
        )
        self.assertEqual(status, 422)
        self.assertTrue(self.disk.exists())
        self.assertTrue(any(item["id"] == self.request_id for item in app.load_requests()))

    @patch.object(app, "local_qemu_domain_exists", return_value=False)
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


if __name__ == "__main__":
    unittest.main()
