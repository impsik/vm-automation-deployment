import http.client
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import app


class AdminPasswordIntegrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.test_directory = Path(tempfile.mkdtemp(prefix="vm-foundry-admin-tests-"))
        cls.original_data_file = app.DATA_FILE
        cls.original_username = app.LOCAL_ADMIN_USERNAME
        cls.original_hash = app.LOCAL_ADMIN_PASSWORD_HASH
        app.DATA_FILE = cls.test_directory / "portal.db"
        app.LOCAL_ADMIN_USERNAME = "admin"
        app.LOCAL_ADMIN_PASSWORD_HASH = app.hash_local_admin_password("original-password")
        app.SESSIONS.clear()
        app.database().close()

        cls.token = "admin-password-test-session"
        app.SESSIONS[cls.token] = {
            "username": "admin",
            "role": "admin",
            "auth_source": "local_admin",
            "expires": time.time() + 3600,
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
        app.SESSIONS.clear()
        app.DATA_FILE = cls.original_data_file
        app.LOCAL_ADMIN_USERNAME = cls.original_username
        app.LOCAL_ADMIN_PASSWORD_HASH = cls.original_hash

    def post(self, payload, token=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Cookie"] = f"vmportal_session={token}"
        connection.request(
            "POST",
            "/api/admin/settings/password",
            body=json.dumps(payload),
            headers=headers,
        )
        response = connection.getresponse()
        body = json.loads(response.read() or b"{}")
        connection.close()
        return response.status, body

    def test_change_password_requires_current_password_and_persists_hash(self):
        other_token = "other-local-admin-session"
        app.SESSIONS[other_token] = {
            "username": "admin",
            "role": "admin",
            "auth_source": "local_admin",
            "expires": time.time() + 3600,
        }
        status, body = self.post(
            {
                "current_password": "original-password",
                "new_password": "replacement-password",
                "confirm_password": "replacement-password",
            },
            self.token,
        )

        self.assertEqual(status, 200)
        self.assertTrue(body["changed"])
        self.assertFalse(app.authenticate_local_admin("admin", "original-password"))
        self.assertTrue(app.authenticate_local_admin("admin", "replacement-password"))
        self.assertIn(self.token, app.SESSIONS)
        self.assertNotIn(other_token, app.SESSIONS)

    def test_ldap_admin_session_cannot_change_local_password(self):
        ldap_token = "ldap-admin-session"
        app.SESSIONS[ldap_token] = {
            "username": "admin",
            "role": "admin",
            "auth_source": "ldap",
            "expires": time.time() + 3600,
        }
        status, _body = self.post(
            {
                "current_password": "replacement-password",
                "new_password": "another-password",
                "confirm_password": "another-password",
            },
            ldap_token,
        )
        self.assertEqual(status, 403)

    def test_password_validation(self):
        status, body = self.post(
            {
                "current_password": "wrong-password",
                "new_password": "short",
                "confirm_password": "different",
            },
            self.token,
        )
        self.assertEqual(status, 422)
        self.assertIn("Current password is incorrect.", body["errors"])
        self.assertIn("New password must be at least 12 characters long.", body["errors"])
        self.assertIn("New password and confirmation do not match.", body["errors"])


if __name__ == "__main__":
    unittest.main()
