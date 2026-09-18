import unittest
from unittest.mock import patch

import app


class NotificationTemplateTest(unittest.TestCase):
    @staticmethod
    def request():
        return {
            "id": "mailtest1",
            "hostname": "mailtest",
            "fqdn": "mailtest.example.test",
            "domain": "example.test",
            "status": "completed",
            "requested_by": "testuser",
            "environment": "development",
            "image": "Ubuntu 22.04 LTS (LVM)",
            "ip_address": "192.0.2.10",
            "ssh_login_user": "ubuntu",
            "resources": {"vcpu": 4, "memory_gb": 8, "disk_gb": 80},
            "additional_disks": [
                {
                    "mountpoint": "/data",
                    "size_gb": 100,
                    "filesystem": "ext4",
                }
            ],
            "events": [],
        }

    def test_created_template_contains_vm_details(self):
        subject, body = app.notification_message("created", self.request())
        self.assertIn("mailtest.example.test", subject)
        self.assertIn("192.0.2.10", body)
        self.assertIn("vCPU: 4", body)
        self.assertIn("/data: 100 GB", body)
        self.assertIn("SSH user: ubuntu", body)

    def test_resize_template_contains_before_and_after(self):
        subject, body = app.notification_message(
            "resized",
            self.request(),
            previous_resources={"vcpu": 2, "memory_gb": 4, "disk_gb": 40},
            previous_disks=[{"mountpoint": "/data", "size_gb": 50}],
        )
        self.assertIn("resources were changed", subject)
        self.assertIn("Previous resources", body)
        self.assertIn("Memory: 4 GB", body)
        self.assertIn("New resources", body)
        self.assertIn("Memory: 8 GB", body)

    def test_failure_template_contains_detailed_reason(self):
        request = self.request()
        request["status"] = "rejected"
        request["rejected_by"] = "admin"
        subject, body = app.notification_message(
            "failed",
            request,
            detail="The request was rejected by admin. Reason: test rejection",
        )
        self.assertIn("failed", subject)
        self.assertIn("test rejection", body)
        self.assertIn("Administrator: admin", body)

    @patch.object(app, "NOTIFICATION_ENABLED", True)
    @patch.object(app, "NOTIFICATION_TO", "admin@example.com")
    @patch.object(app.smtplib, "SMTP")
    def test_send_uses_configured_smtp_and_records_success(self, smtp):
        request = self.request()
        self.assertTrue(app.send_notification("created", request))
        smtp.assert_called_once_with(app.SMTP_HOST, app.SMTP_PORT, timeout=10)
        smtp.return_value.__enter__.return_value.send_message.assert_called_once()
        self.assertEqual(request["notification_history"][0]["status"], "sent")
        self.assertEqual(request["events"][-1]["name"], "Email notification sent")


if __name__ == "__main__":
    unittest.main()
