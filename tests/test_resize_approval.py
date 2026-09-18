import http.client
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import app


class ResizeApprovalIntegrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        backend = patch.object(app, 'PROVISIONING_BACKEND', 'local_qemu')
        backend.start()
        cls.addClassCleanup(backend.stop)
        cls.test_directory = Path(tempfile.mkdtemp(prefix="vm-foundry-tests-"))
        app.DATA_FILE = cls.test_directory / "portal.db"
        app.SESSIONS.clear()
        app.database().close()

        cls.user_token = "test-user-session"
        cls.admin_token = "test-admin-session"
        expires = time.time() + 3600
        app.SESSIONS[cls.user_token] = {
            "username": "regular-user",
            "role": "user",
            "auth_source": "test",
            "expires": expires,
        }
        app.SESSIONS[cls.admin_token] = {
            "username": "admin",
            "role": "admin",
            "auth_source": "test",
            "expires": expires,
        }
        cls.server = app.ThreadingHTTPServer(("127.0.0.1", 0), app.PortalHandler)
        cls.server_thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.server_thread.start()
        cls.port = cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.server_thread.join(timeout=5)

    @staticmethod
    def base_request(request_id="resize01"):
        return {
            "id": request_id,
            "created_at": app.now(),
            "status": "completed",
            "hostname": request_id,
            "fqdn": f"{request_id}.example.test",
            "domain": "example.test",
            "environment": "development",
            "image": next(iter(app.LOCAL_QEMU.get("images", {})), "Ubuntu"),
            "network": next(iter(app.NETWORKS_BY_PORTGROUP), "default"),
            "resources": {"vcpu": 8, "memory_gb": 8, "disk_gb": 80},
            "additional_disks": [
                {
                    "index": 1,
                    "size_gb": 100,
                    "mountpoint": "/data",
                    "target": "vdb",
                    "storage_file": f"{request_id}-data-1.qcow2",
                    "vg_name": "vg_data_1",
                    "lv_name": "lv_data",
                }
            ],
            "requested_by": "regular-user",
            "events": [app.event("Test VM created")],
        }

    def setUp(self):
        self.request_id = f"resize-{self._testMethodName[-24:].replace('_', '-')}"
        app.save_requests([self.base_request(self.request_id)])

    def request(self, method, path, token, payload=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        body = json.dumps(payload).encode() if payload is not None else None
        headers = {"Cookie": f"vmportal_session={token}"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        response_body = json.loads(response.read() or b"{}")
        connection.close()
        return response.status, response_body

    @staticmethod
    def resize_payload(vcpu=8, memory_gb=8, disk_gb=80, data_disk_gb=100):
        return {
            "vcpu": vcpu,
            "memory_gb": memory_gb,
            "disk_gb": disk_gb,
            "additional_disks": [{"size_gb": data_disk_gb}],
        }

    @staticmethod
    def fake_resize(request, resources, data_disks, requested_by):
        request["resources"] = resources
        request["additional_disks"] = data_disks
        request["events"].append(app.event("Fake online resize completed", detail=requested_by))
        return True, ""

    @staticmethod
    def matching_request(requests, request_id):
        return next(item for item in requests if item["id"] == request_id)

    @patch.object(app, "local_qemu_domain_exists", return_value=True)
    @patch.object(app, "resize_local_qemu")
    @patch.object(app, "preflight_local_resize", return_value=[])
    def test_current_sizes_can_be_submitted_to_reconcile_offline_cpus(
        self, preflight, resize, _domain_exists
    ):
        resize.side_effect = self.fake_resize
        status, body = self.request(
            "POST", f"/api/requests/{self.request_id}/resize",
            self.user_token, self.resize_payload(),
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["resources"]["vcpu"], 8)
        resize.assert_called_once()

    @patch.object(app, "local_qemu_domain_exists", return_value=True)
    @patch.object(app, "resize_local_qemu")
    @patch.object(app, "preflight_local_resize", return_value=[])
    def test_within_limit_resize_runs_immediately(
        self, preflight, resize, _domain_exists
    ):
        resize.side_effect = self.fake_resize
        status, body = self.request(
            "POST",
            f"/api/requests/{self.request_id}/resize",
            self.user_token,
            self.resize_payload(memory_gb=16),
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "completed")
        self.assertEqual(body["resources"]["memory_gb"], 16)
        preflight.assert_called_once()
        resize.assert_called_once()

    @patch.object(app, "local_qemu_domain_exists", return_value=True)
    @patch.object(app, "resize_local_qemu")
    @patch.object(
        app,
        "preflight_local_resize",
        side_effect=AssertionError("preflight must wait for administrator approval"),
    )
    def test_over_limit_resize_is_visible_to_admin_before_preflight(
        self, preflight, resize, _domain_exists
    ):
        status, body = self.request(
            "POST",
            f"/api/requests/{self.request_id}/resize",
            self.user_token,
            self.resize_payload(vcpu=12),
        )
        self.assertEqual(status, 202)
        self.assertEqual(body["status"], "resize_awaiting_approval")
        self.assertEqual(body["pending_resize"]["resources"]["vcpu"], 12)
        self.assertTrue(body["pending_resize"]["approval_reasons"])
        preflight.assert_not_called()
        resize.assert_not_called()

        status, requests = self.request(
            "GET", "/api/requests", self.admin_token
        )
        self.assertEqual(status, 200)
        request = self.matching_request(requests, self.request_id)
        self.assertEqual(request["status"], "resize_awaiting_approval")
        self.assertEqual(request["pending_resize"]["resources"]["vcpu"], 12)

    @patch.object(app, "send_notification")
    @patch.object(app, "local_qemu_domain_exists", return_value=True)
    def test_admin_reject_keeps_current_resources(self, _domain_exists, notification):
        status, _body = self.request(
            "POST",
            f"/api/requests/{self.request_id}/resize",
            self.user_token,
            self.resize_payload(vcpu=12),
        )
        self.assertEqual(status, 202)

        status, body = self.request(
            "POST",
            f"/api/requests/{self.request_id}/reject",
            self.admin_token,
            {"reason": "Capacity increase was not approved."},
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "completed")
        self.assertEqual(body["resources"]["vcpu"], 8)
        self.assertEqual(body["resize_rejected_by"], "admin")
        self.assertNotIn("pending_resize", body)
        notification.assert_called_once()
        self.assertIn("Capacity increase", notification.call_args.kwargs["detail"])

    @patch.object(app, "local_qemu_domain_exists", return_value=True)
    @patch.object(app, "resize_local_qemu")
    @patch.object(app, "preflight_local_resize", return_value=[])
    def test_admin_approve_runs_preflight_and_resize(
        self, preflight, resize, _domain_exists
    ):
        resize.side_effect = self.fake_resize
        status, _body = self.request(
            "POST",
            f"/api/requests/{self.request_id}/resize",
            self.user_token,
            self.resize_payload(vcpu=12),
        )
        self.assertEqual(status, 202)
        preflight.assert_not_called()

        status, body = self.request(
            "POST",
            f"/api/requests/{self.request_id}/approve",
            self.admin_token,
            {},
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "completed")
        self.assertEqual(body["resources"]["vcpu"], 12)
        self.assertEqual(body["resize_approved_by"], "admin")
        self.assertNotIn("pending_resize", body)
        preflight.assert_called_once()
        self.assertFalse(preflight.call_args.kwargs["retain_reserve"])
        resize.assert_called_once()

    @patch.object(app.os, "statvfs")
    def test_admin_approved_resize_can_use_reserved_space(self, statvfs):
        filesystem = type("Filesystem", (), {})()
        filesystem.f_blocks = 915
        filesystem.f_bavail = 587
        filesystem.f_frsize = app.GIB
        statvfs.return_value = filesystem
        request = self.base_request(self.request_id)
        resources = {**request["resources"], "disk_gb": 501}

        regular_errors = app.preflight_local_resize(
            request,
            resources,
            request["additional_disks"],
        )
        approved_errors = app.preflight_local_resize(
            request,
            resources,
            request["additional_disks"],
            retain_reserve=False,
        )

        self.assertEqual(regular_errors, [app.PUBLIC_PREFLIGHT_ERROR])
        self.assertEqual(approved_errors, [])

    @patch.object(app, "local_qemu_domain_exists", return_value=True)
    @patch.object(app, "resize_local_qemu")
    def test_cpu_decrease_is_still_rejected(self, resize, _domain_exists):
        status, body = self.request(
            "POST", f"/api/requests/{self.request_id}/resize",
            self.user_token, self.resize_payload(vcpu=4),
        )
        self.assertEqual(status, 422)
        self.assertIn("cannot be reduced", body["errors"][0])
        resize.assert_not_called()

    @patch.object(app, "local_qemu_domain_exists", return_value=True)
    def test_hard_limit_is_rejected_without_pending_request(self, _domain_exists):
        status, body = self.request(
            "POST",
            f"/api/requests/{self.request_id}/resize",
            self.user_token,
            self.resize_payload(vcpu=33),
        )
        self.assertEqual(status, 422)
        self.assertIn("hard limit", body["errors"][0])

        status, requests = self.request(
            "GET", "/api/requests", self.admin_token
        )
        self.assertEqual(status, 200)
        request = self.matching_request(requests, self.request_id)
        self.assertEqual(request["status"], "completed")
        self.assertNotIn("pending_resize", request)


if __name__ == "__main__":
    unittest.main()
