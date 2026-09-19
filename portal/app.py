#!/usr/bin/env python3
"""Dependency-free API for the VM self-service portal PoC."""

from __future__ import annotations

import json
import os
import re
import secrets
import select
import smtplib
import socket
import sqlite3
import struct
import subprocess
import threading
import time
import uuid
import xml.etree.ElementTree as ET
import base64
import binascii
import hashlib
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import yaml
from ldap3 import Connection, Server
from netbox_client import NetBoxClient, NetBoxError


PORT = int(os.getenv("PORT", "8080"))
DATA_FILE = Path(os.getenv("DATA_FILE", "./data/portal.db"))
STATIC_DIR = Path(__file__).parent / "static"
CONFIG_FILE = Path(os.getenv("CONFIG_FILE", "./config.yml"))
LOCK = threading.Lock()
SESSIONS: dict[str, dict] = {}
CONSOLE_TICKETS: dict[str, dict] = {}
VM_DELETE_TICKETS: dict[str, dict] = {}
LOCAL_ADMIN_USERNAME = os.getenv("LOCAL_ADMIN_USERNAME", "").strip()
LOCAL_ADMIN_PASSWORD_HASH = os.getenv("LOCAL_ADMIN_PASSWORD_HASH", "").strip()
LOCAL_ADMIN_PASSWORD_ITERATIONS = 600_000
SNAPSHOT_DELETE_PLAN_PATH = re.compile(
    r"/api/requests/([a-z0-9-]+)/snapshots/([A-Za-z0-9-]+)/delete-plan"
)
RESIZE_SNAPSHOT_CLEANUP_PLAN_PATH = re.compile(
    r"/api/requests/([a-z0-9-]+)/resize-snapshots/([A-Za-z0-9-]+)/cleanup-plan"
)
VM_DELETE_PLAN_PATH = re.compile(r"/api/requests/([a-z0-9-]+)/delete-plan")
VM_DELETE_PATH = re.compile(r"/api/requests/([a-z0-9-]+)")
RESIZE_SNAPSHOT_TTL = timedelta(hours=12)


def load_config() -> dict:
    with CONFIG_FILE.open(encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


CONFIG = load_config()
NETBOX = CONFIG.get('netbox', {})
NETBOX_ENABLED = (os.getenv('NETBOX_ENABLED') or str(NETBOX.get('enabled', False))).lower() in ('true', '1', 'yes')
NETBOX_URL = os.getenv('NETBOX_URL') or NETBOX.get('url', '')
NETBOX_TOKEN = os.getenv('NETBOX_TOKEN', '')
NETBOX_CLUSTER_ID = os.getenv('NETBOX_CLUSTER_ID') or NETBOX.get('cluster_id')

PROFILES = {
    "small": {"label": "Small", "vcpu": 2, "memory_gb": 4, "disk_gb": 40},
    "medium": {"label": "Medium", "vcpu": 4, "memory_gb": 8, "disk_gb": 80},
    "large": {"label": "Large", "vcpu": 8, "memory_gb": 32, "disk_gb": 200},
}

POLICY = {
    "automatic": {"vcpu": 8, "memory_gb": 32, "disk_gb": 500},
    "hard": {"vcpu": 32, "memory_gb": 512, "disk_gb": 8000},
}

ENVIRONMENTS = {
    "development": {"label": "Development", "approval": False},
    "test": {"label": "Test", "approval": False},
    "production": {"label": "Production", "approval": True},
}

PROVISIONING = CONFIG.get("provisioning", {})
PROVISIONING_BACKEND = PROVISIONING.get("backend", "vcsim")
LOCAL_QEMU = PROVISIONING.get("local_qemu", {})
IMAGES = list(LOCAL_QEMU.get("images", {})) if PROVISIONING_BACKEND == "local_qemu" else ["Ubuntu 24.04 LTS", "Rocky Linux 9", "Debian 12"]
NETWORKS = CONFIG.get("networks", [])
NETWORKS_BY_PORTGROUP = {
    str(network["portgroup"]): network
    for network in NETWORKS
    if isinstance(network, dict) and network.get("portgroup")
}
MAX_ADDITIONAL_DISK_GB = 8000
MAX_ADDITIONAL_DISKS = 20
DATASTORE_RESERVE_PERCENT = 20
GIB = 1024 ** 3
SUPPORT_EMAIL = CONFIG.get("support", {}).get("email", "it.taristu@domeen.ee")
HOSTMASTER = CONFIG.get("hostmaster", {})
HOSTMASTER_EMAIL = str(HOSTMASTER.get("email", "hostmaster@domeen.ee")).strip()
HOSTMASTER_DESCRIPTION = str(
    HOSTMASTER.get("description", "Please register the new virtual machine DNS name.")
).strip()
NOTIFICATIONS = CONFIG.get("notifications", {})
NOTIFICATION_ENABLED = bool(NOTIFICATIONS.get("enabled", False))
NOTIFICATION_TO = str(NOTIFICATIONS.get("to", "")).strip()
NOTIFICATION_FROM = str(
    NOTIFICATIONS.get("from", "vm-foundry@localhost")
).strip()
SMTP_HOST = str(NOTIFICATIONS.get("smtp_host", "127.0.0.1")).strip()
SMTP_PORT = int(NOTIFICATIONS.get("smtp_port", 25))
SSH_LOGIN_USER = str(LOCAL_QEMU.get("ssh_user", "ubuntu")).strip()
PUBLIC_PREFLIGHT_ERROR = (
    "A virtual machine with these parameters cannot be provisioned automatically. "
    f"Please send the request to {SUPPORT_EMAIL}."
)
DUPLICATE_REQUEST_ERROR = (
    "A virtual machine or active request with this hostname already exists. "
    "Please check your requests view."
)


class ClosingConnection(sqlite3.Connection):
    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def database() -> sqlite3.Connection:
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DATA_FILE, factory=ClosingConnection)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("""
        CREATE TABLE IF NOT EXISTS requests (
            id TEXT PRIMARY KEY,
            created_at TEXT NOT NULL,
            status TEXT NOT NULL,
            data TEXT NOT NULL
        )
    """)
    connection.execute("""
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            request_id TEXT NOT NULL REFERENCES requests(id) ON DELETE CASCADE,
            sequence INTEGER NOT NULL,
            timestamp TEXT NOT NULL,
            name TEXT NOT NULL,
            state TEXT NOT NULL,
            detail TEXT NOT NULL
        )
    """)
    connection.execute("""
        CREATE TABLE IF NOT EXISTS preflight_checks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            hostname TEXT NOT NULL,
            requested_disk_gb INTEGER NOT NULL,
            result TEXT NOT NULL,
            detail TEXT NOT NULL
        )
    """)
    connection.execute("""
        CREATE TABLE IF NOT EXISTS authentication_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            username TEXT NOT NULL,
            source_address TEXT NOT NULL,
            result TEXT NOT NULL,
            detail TEXT NOT NULL
        )
    """)
    connection.execute("""
        CREATE TABLE IF NOT EXISTS ssh_keys (
            id TEXT PRIMARY KEY,
            username TEXT NOT NULL,
            login_user TEXT NOT NULL,
            label TEXT NOT NULL,
            public_key TEXT NOT NULL,
            fingerprint TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    """)
    connection.execute("""
        CREATE TABLE IF NOT EXISTS local_admin_credentials (
            username TEXT PRIMARY KEY,
            password_hash TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    ssh_key_columns = {
        row["name"] for row in connection.execute("PRAGMA table_info(ssh_keys)")
    }
    if "login_user" not in ssh_key_columns:
        connection.execute(
            "ALTER TABLE ssh_keys ADD COLUMN login_user TEXT NOT NULL DEFAULT 'ubuntu'"
        )
    return connection


def authenticate_ldap(username: str, password: str) -> tuple[bool, str]:
    auth = CONFIG.get("authentication", {})
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", username) or not password:
        return False, "invalid_credentials"
    ldap_endpoint = urlparse(auth["ldap_url"])
    use_ssl = bool(auth.get("use_ssl", False)) or ldap_endpoint.scheme == "ldaps"
    server = Server(
        ldap_endpoint.hostname or auth["ldap_url"],
        port=ldap_endpoint.port or (636 if use_ssl else 389),
        use_ssl=use_ssl,
        connect_timeout=int(auth.get("connect_timeout_seconds", 8)),
    )
    user_dn = str(auth["user_dn_pattern"]).format(username=username)
    try:
        connection = Connection(server, user=user_dn, password=password, raise_exceptions=False)
        connection.open()
        if connection.closed:
            return False, "ldap_unavailable"
        if auth.get("start_tls") and not connection.start_tls():
            return False, "tls_failed"
        if not connection.bind():
            return False, "invalid_credentials"
        connection.unbind()
        return True, "authenticated"
    except Exception:
        return False, "ldap_unavailable"


def local_admin_password_hash(username: str) -> str:
    with database() as connection:
        row = connection.execute(
            "SELECT password_hash FROM local_admin_credentials WHERE username = ?",
            (username,),
        ).fetchone()
    return row["password_hash"] if row else LOCAL_ADMIN_PASSWORD_HASH


def hash_local_admin_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        LOCAL_ADMIN_PASSWORD_ITERATIONS,
    )
    return (
        f"pbkdf2_sha256${LOCAL_ADMIN_PASSWORD_ITERATIONS}$"
        f"{salt.hex()}${digest.hex()}"
    )


def authenticate_local_admin(username: str, password: str) -> bool:
    if not LOCAL_ADMIN_USERNAME or not password:
        return False
    if not secrets.compare_digest(username, LOCAL_ADMIN_USERNAME):
        return False
    password_hash = local_admin_password_hash(username)
    if not password_hash:
        return False
    try:
        algorithm, iterations_text, salt_hex, expected_hex = password_hash.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        actual = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            bytes.fromhex(salt_hex),
            int(iterations_text),
        )
        return secrets.compare_digest(actual.hex(), expected_hex)
    except (TypeError, ValueError):
        return False


def log_authentication(username: str, address: str, result: str, detail: str = "") -> None:
    with database() as connection:
        connection.execute(
            "INSERT INTO authentication_events(timestamp, username, source_address, result, detail) VALUES (?, ?, ?, ?, ?)",
            (now(), username, address, result, detail),
        )


def user_role(username: str) -> str:
    admin_users = CONFIG.get("authentication", {}).get("admin_users", [])
    return "admin" if username.casefold() in {str(user).casefold() for user in admin_users} else "user"


def load_requests() -> list[dict]:
    with database() as connection:
        rows = connection.execute("SELECT * FROM requests ORDER BY created_at DESC").fetchall()
        requests = []
        for row in rows:
            item = json.loads(row["data"])
            item.update(id=row["id"], created_at=row["created_at"], status=row["status"])
            item["events"] = [dict(event_row) | {"id": event_row["id"]} for event_row in connection.execute(
                "SELECT id, timestamp, name, state, detail FROM events WHERE request_id = ? ORDER BY sequence",
                (row["id"],),
            )]
            requests.append(item)
        return requests


def save_requests(requests: list[dict]) -> None:
    with database() as connection:
        for request in requests:
            stored = {key: value for key, value in request.items() if key not in {"id", "created_at", "status", "events"}}
            connection.execute(
                "INSERT OR REPLACE INTO requests(id, created_at, status, data) VALUES (?, ?, ?, ?)",
                (request["id"], request["created_at"], request["status"], json.dumps(stored)),
            )
            connection.execute("DELETE FROM events WHERE request_id = ?", (request["id"],))
            connection.executemany(
                "INSERT INTO events(request_id, sequence, timestamp, name, state, detail) VALUES (?, ?, ?, ?, ?, ?)",
                [(request["id"], sequence, item["timestamp"], item["name"], item["state"], item["detail"])
                 for sequence, item in enumerate(request["events"])],
            )


def event(name: str, state: str = "done", detail: str = "") -> dict:
    return {"name": name, "state": state, "detail": detail, "timestamp": now()}


def resource_lines(resources: dict) -> list[str]:
    return [
        f"vCPU: {resources.get('vcpu', '-')}",
        f"Memory: {resources.get('memory_gb', '-')} GB",
        f"OS disk: {resources.get('disk_gb', '-')} GB",
    ]


def data_disk_lines(request: dict) -> list[str]:
    disks = request_data_disks(request)
    if not disks:
        return ["Additional disks: none"]
    return [
        "Additional disks:",
        *[
            f"  - {disk.get('mountpoint', '-')}: {disk.get('size_gb', '-')} GB "
            f"({disk.get('filesystem', 'ext4')})"
            for disk in disks
        ],
    ]


def notification_message(
    kind: str,
    request: dict,
    *,
    detail: str = "",
    previous_resources: dict | None = None,
    previous_disks: list[dict] | None = None,
) -> tuple[str, str]:
    fqdn = request.get("fqdn") or (
        f"{request.get('hostname', '-')}.{request.get('domain', '')}".rstrip(".")
    )
    common = [
        f"Request ID: {request.get('id', '-')}",
        f"Virtual machine: {fqdn}",
        f"Requested by: {request.get('requested_by', '-')}",
        f"Environment: {request.get('environment', '-')}",
        f"Operating system: {request.get('image', '-')}",
    ]
    if kind == "created":
        subject = f"[VM Foundry] Virtual machine {fqdn} has been created"
        body = [
            "The virtual machine request was completed successfully.",
            "",
            *common,
            f"IP address: {request.get('ip_address', 'DHCP address is still pending')}",
            f"SSH user: {request.get('ssh_login_user', SSH_LOGIN_USER)}",
            "",
            "Resources:",
            *resource_lines(request.get("resources", {})),
            *data_disk_lines(request),
        ]
    elif kind == "resized":
        subject = f"[VM Foundry] Virtual machine {fqdn} resources were changed"
        body = [
            "The virtual machine resources were changed successfully.",
            "",
            *common,
            "",
            "Previous resources:",
            *resource_lines(previous_resources or {}),
            *(
                ["Additional disks:", *[
                    f"  - {disk.get('mountpoint', '-')}: {disk.get('size_gb', '-')} GB"
                    for disk in (previous_disks or [])
                ]]
                if previous_disks else ["Additional disks: none"]
            ),
            "",
            "New resources:",
            *resource_lines(request.get("resources", {})),
            *data_disk_lines(request),
            "",
            "The change was applied online; the VM was not rebooted.",
        ]
    else:
        subject = f"[VM Foundry] Virtual machine request for {fqdn} failed"
        body = [
            "The virtual machine request or resource change failed.",
            "",
            *common,
            f"Status: {request.get('status', 'failed')}",
            f"Administrator: {request.get('rejected_by') or request.get('resize_rejected_by') or '-'}",
            "",
            "Reason:",
            detail or "No detailed reason is available. Please review the request event log.",
        ]
    return subject, "\n".join(body) + "\n"


def send_notification(
    kind: str,
    request: dict,
    *,
    detail: str = "",
    previous_resources: dict | None = None,
    previous_disks: list[dict] | None = None,
) -> bool:
    if not NOTIFICATION_ENABLED or not NOTIFICATION_TO:
        return False
    subject, body = notification_message(
        kind,
        request,
        detail=detail,
        previous_resources=previous_resources,
        previous_disks=previous_disks,
    )
    message = EmailMessage()
    message["From"] = NOTIFICATION_FROM
    message["To"] = NOTIFICATION_TO
    message["Subject"] = subject
    message.set_content(body)
    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=10) as smtp:
            smtp.send_message(message)
        request.setdefault("notification_history", []).append({
            "timestamp": now(),
            "kind": kind,
            "to": NOTIFICATION_TO,
            "subject": subject,
            "status": "sent",
        })
        request["events"].append(event(
            "Email notification sent",
            detail=f"To: {NOTIFICATION_TO}; subject: {subject}",
        ))
        return True
    except (OSError, smtplib.SMTPException) as error:
        request.setdefault("notification_history", []).append({
            "timestamp": now(),
            "kind": kind,
            "to": NOTIFICATION_TO,
            "subject": subject,
            "status": "failed",
            "detail": str(error),
        })
        request["events"].append(event(
            "Email notification failed",
            "failed",
            str(error),
        ))
        return False


def user_ssh_keys(username: str) -> list[dict]:
    with database() as connection:
        return [
            dict(row) for row in connection.execute(
                "SELECT id, label, public_key, fingerprint, created_at "
                ", login_user "
                "FROM ssh_keys WHERE username = ? ORDER BY created_at, label",
                (username,),
            )
        ]


def validate_public_key(value: object) -> tuple[str | None, str | None]:
    public_key = " ".join(str(value or "").strip().split())
    parts = public_key.split(" ", 2)
    allowed_types = {
        "ssh-ed25519", "ssh-rsa",
        "ecdsa-sha2-nistp256", "ecdsa-sha2-nistp384", "ecdsa-sha2-nistp521",
        "sk-ssh-ed25519@openssh.com", "sk-ecdsa-sha2-nistp256@openssh.com",
    }
    if len(parts) < 2 or parts[0] not in allowed_types:
        return None, "Unsupported or invalid OpenSSH public key."
    try:
        decoded = base64.b64decode(parts[1], validate=True)
    except (binascii.Error, ValueError):
        return None, "SSH public key contains invalid base64 data."
    if len(decoded) < 32 or len(public_key) > 16384:
        return None, "SSH public key has an invalid length."
    type_length = int.from_bytes(decoded[:4], "big")
    try:
        encoded_type = decoded[4:4 + type_length].decode("ascii")
    except UnicodeDecodeError:
        return None, "SSH public key has an invalid binary format."
    if encoded_type != parts[0]:
        return None, "SSH public key type does not match its encoded data."
    fingerprint = base64.b64encode(hashlib.sha256(decoded).digest()).decode().rstrip("=")
    return public_key, f"SHA256:{fingerprint}"


def simulate_hostmaster_email(request: dict) -> None:
    """Record the hostmaster message without sending anything externally."""
    fqdn = f"{request['hostname']}.{request['domain']}"
    body = (
        f"{HOSTMASTER_DESCRIPTION}\n"
        f"Requested DNS name: {fqdn}\n"
        f"VM name: {request['hostname']}\n"
        f"Domain: {request['domain']}"
    )
    if request.get("ip_address"):
        body += f"\nIP address: {request['ip_address']}"
    request["hostmaster_email"] = {
        "mode": "simulation",
        "to": HOSTMASTER_EMAIL,
        "subject": f"DNS registration request: {fqdn}",
        "body": body,
    }
    request["events"].append(event(
        "Hostmaster email simulated",
        detail=f"To: {HOSTMASTER_EMAIL}; requested DNS name: {fqdn}",
    ))


def preflight_vcsim(hostname: str, resources: dict) -> tuple[dict | None, list[str]]:
    """Find a datastore that can satisfy the request while retaining reserve."""
    environment = os.environ.copy()
    environment.update({
        "GOVC_URL": (
            f"https://{os.getenv('VCENTER_USERNAME', 'admin')}:"
            f"{os.getenv('VCENTER_PASSWORD', 'test-password')}@"
            f"{os.getenv('VCENTER_HOST', '127.0.0.1')}:"
            f"{os.getenv('VCENTER_PORT', '8989')}"
        ),
        "GOVC_INSECURE": "1",
        "GOVC_DATACENTER": "DC0",
    })
    try:
        result = subprocess.run(
            ["govc", "datastore.info", "-json"],
            capture_output=True, text=True, timeout=30, check=False, env=environment,
        )
        if result.returncode:
            detail = f"Infrastructure preflight failed: {result.stderr.strip() or result.stdout.strip()}"
            log_preflight(hostname, resources["disk_gb"], "error", detail)
            return None, [PUBLIC_PREFLIGHT_ERROR]
        datastores = json.loads(result.stdout).get("datastores", [])
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as error:
        detail = f"Infrastructure preflight failed: {error}"
        log_preflight(hostname, resources["disk_gb"], "error", detail)
        return None, [PUBLIC_PREFLIGHT_ERROR]

    requested_bytes = resources["disk_gb"] * GIB
    candidates = []
    inventory = []
    for datastore in datastores:
        summary = datastore.get("summary") or {}
        capacity = int(summary.get("capacity") or 0)
        free = int(summary.get("freeSpace") or 0)
        reserve = capacity * DATASTORE_RESERVE_PERCENT // 100
        usable_free = max(0, free - reserve)
        inventory.append(f"{datastore.get('name')}: {free // GIB} GB free, {usable_free // GIB} GB usable")
        if requested_bytes <= usable_free:
            candidates.append((usable_free, datastore.get("name"), capacity, free, reserve))

    if not candidates:
        detail = (
            f"No datastore can fit {resources['disk_gb']} GB while retaining "
            f"{DATASTORE_RESERVE_PERCENT}% reserve. " + "; ".join(inventory)
        )
        log_preflight(hostname, resources["disk_gb"], "rejected", detail)
        return None, [PUBLIC_PREFLIGHT_ERROR]

    usable_free, name, capacity, free, reserve = max(candidates)
    placement = {
        "datacenter": "DC0",
        "datastore": name,
        "capacity_gb": capacity // GIB,
        "free_before_gb": free // GIB,
        "reserve_gb": reserve // GIB,
        "free_after_gb": (free - requested_bytes) // GIB,
        "resource_pool": "/DC0/host/DC0_C0/Resources",
    }
    log_preflight(hostname, resources["disk_gb"], "passed", json.dumps(placement))
    return placement, []


def preflight_local_qemu(hostname: str, resources: dict, image: str) -> tuple[dict | None, list[str]]:
    storage_path = Path(LOCAL_QEMU["storage_path"])
    base_image = Path(LOCAL_QEMU.get("images", {}).get(image, ""))
    try:
        storage_path.mkdir(parents=True, exist_ok=True)
        if not base_image.is_file():
            raise FileNotFoundError(f"Configured base image does not exist: {base_image}")
        if local_qemu_domain_exists(hostname):
            detail = f"Local libvirt domain already exists: {hostname}"
            log_preflight(hostname, resources["disk_gb"], "rejected", detail)
            return None, [DUPLICATE_REQUEST_ERROR]
        filesystem = os.statvfs(storage_path)
        capacity = filesystem.f_blocks * filesystem.f_frsize
        free = filesystem.f_bavail * filesystem.f_frsize
    except (OSError, subprocess.TimeoutExpired) as error:
        detail = f"Local QEMU preflight failed: {error}"
        log_preflight(hostname, resources["disk_gb"], "error", detail)
        return None, [PUBLIC_PREFLIGHT_ERROR]

    reserve_percent = int(LOCAL_QEMU.get("reserve_percent", DATASTORE_RESERVE_PERCENT))
    reserve = capacity * reserve_percent // 100
    requested_bytes = resources["disk_gb"] * GIB
    usable_free = max(0, free - reserve)
    if requested_bytes > usable_free:
        detail = (
            f"Local QEMU storage cannot fit {resources['disk_gb']} GB while retaining "
            f"{reserve_percent}% reserve: {free // GIB} GB free, {usable_free // GIB} GB usable"
        )
        log_preflight(hostname, resources["disk_gb"], "rejected", detail)
        return None, [PUBLIC_PREFLIGHT_ERROR]

    placement = {
        "backend": "local_qemu",
        "datacenter": "LOCAL-QEMU",
        "datastore": "Local QEMU storage",
        "capacity_gb": capacity // GIB,
        "free_before_gb": free // GIB,
        "reserve_gb": reserve // GIB,
        "free_after_gb": (free - requested_bytes) // GIB,
        "resource_pool": "qemu:///system",
        "storage_path": str(storage_path),
        "base_image": str(base_image),
    }
    log_preflight(hostname, resources["disk_gb"], "passed", json.dumps(placement))
    return placement, []


def local_qemu_domain_exists(hostname: str) -> bool:
    """Return whether libvirt currently has a domain with this hostname."""
    try:
        result = subprocess.run(
            ["virsh", "--connect", LOCAL_QEMU.get("uri", "qemu:///system"), "dominfo", hostname],
            capture_output=True, text=True, timeout=15, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def local_qemu_domain_state(hostname: str) -> str:
    try:
        result = subprocess.run(
            ["virsh", "--connect", LOCAL_QEMU.get("uri", "qemu:///system"),
             "domstate", hostname],
            capture_output=True, text=True, timeout=15, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"
    return result.stdout.strip().lower() if result.returncode == 0 else "missing"


def local_qemu_spice_target(hostname: str) -> tuple[str, int] | None:
    """Return the loopback SPICE endpoint assigned to a running libvirt domain."""
    try:
        result = subprocess.run(
            [
                "virsh", "--connect", LOCAL_QEMU.get("uri", "qemu:///system"),
                "domdisplay", hostname, "--type", "spice",
            ],
            capture_output=True, text=True, timeout=15, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode:
        return None
    display = urlparse(result.stdout.strip())
    if display.scheme != "spice" or display.hostname not in {"127.0.0.1", "localhost", "::1"}:
        return None
    return display.hostname, display.port


def websocket_recv_exact(connection: socket.socket, length: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < length:
        chunk = connection.recv(length - len(chunks))
        if not chunk:
            raise ConnectionError("WebSocket client disconnected")
        chunks.extend(chunk)
    return bytes(chunks)


def websocket_receive(connection: socket.socket) -> tuple[int, bytes]:
    header = websocket_recv_exact(connection, 2)
    opcode = header[0] & 0x0f
    masked = bool(header[1] & 0x80)
    length = header[1] & 0x7f
    if length == 126:
        length = struct.unpack("!H", websocket_recv_exact(connection, 2))[0]
    elif length == 127:
        length = struct.unpack("!Q", websocket_recv_exact(connection, 8))[0]
    if length > 16 * 1024 * 1024:
        raise ValueError("WebSocket frame is too large")
    mask = websocket_recv_exact(connection, 4) if masked else b""
    payload = websocket_recv_exact(connection, length)
    if masked:
        payload = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
    return opcode, payload


def websocket_send(connection: socket.socket, opcode: int, payload: bytes = b"") -> None:
    header = bytearray([0x80 | opcode])
    length = len(payload)
    if length < 126:
        header.append(length)
    elif length < 65536:
        header.extend((126,))
        header.extend(struct.pack("!H", length))
    else:
        header.extend((127,))
        header.extend(struct.pack("!Q", length))
    connection.sendall(header + payload)


def lifecycle_action(request: dict, action: str, requested_by: str) -> tuple[bool, str]:
    hostname = request["hostname"]
    state = local_qemu_domain_state(hostname)
    commands = {
        "start": ["start", hostname],
        "stop": ["shutdown", hostname, "--mode", "agent,acpi"],
        "reboot": ["reboot", hostname, "--mode", "agent,acpi"],
    }
    if action not in commands:
        return False, "Unsupported lifecycle action"
    if action == "start" and state != "shut off":
        return False, f"VM must be shut off before start; current state: {state}"
    if action in {"stop", "reboot"} and state != "running":
        return False, f"VM must be running; current state: {state}"
    request["events"].append(event(
        f"VM {action} requested",
        "waiting",
        f"Requested by {requested_by}; previous state: {state}",
    ))
    try:
        result = subprocess.run(
            ["virsh", "--connect", LOCAL_QEMU.get("uri", "qemu:///system"),
             *commands[action]],
            capture_output=True, text=True, timeout=30, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        detail = str(error)
        request["events"].append(event(f"VM {action} failed", "failed", detail))
        return False, detail
    if result.returncode:
        detail = (result.stderr or result.stdout).strip() or "virsh returned an error"
        request["events"].append(event(f"VM {action} failed", "failed", detail))
        return False, detail
    expected = "running" if action in {"start", "reboot"} else "shut off"
    deadline = time.time() + (45 if action == "stop" else 20)
    current = local_qemu_domain_state(hostname)
    while current != expected and time.time() < deadline:
        time.sleep(1)
        current = local_qemu_domain_state(hostname)
    if current != expected:
        detail = f"Command was accepted, but VM state is {current}; expected {expected}"
        request["events"].append(event(f"VM {action} pending", "waiting", detail))
        return False, detail
    request["events"].append(event(
        f"VM {action} completed",
        detail=f"Requested by {requested_by}; current state: {current}",
    ))
    return True, current


def domain_disk_files(hostname: str) -> list[dict]:
    result = subprocess.run(
        ["virsh", "--connect", LOCAL_QEMU.get("uri", "qemu:///system"),
         "dumpxml", hostname],
        capture_output=True, text=True, timeout=20, check=True,
    )
    root = ET.fromstring(result.stdout)
    disks = []
    for disk in root.findall("./devices/disk[@device='disk']"):
        source = disk.find("source")
        target = disk.find("target")
        if source is None or target is None or not source.get("file") or not target.get("dev"):
            continue
        disks.append({"target": target.get("dev"), "source": source.get("file")})
    return disks


def deletion_domain_exists(hostname: str) -> bool:
    """Only a successful domain listing can establish absence for deletion."""
    result = subprocess.run(
        ["virsh", "--connect", LOCAL_QEMU.get("uri", "qemu:///system"),
         "list", "--all", "--name"],
        capture_output=True, text=True, timeout=15, check=False,
    )
    if result.returncode:
        raise RuntimeError("Unable to verify VM absence in libvirt; deletion blocked")
    return hostname in result.stdout.splitlines()


def vm_delete_plan(request: dict) -> tuple[dict | None, str]:
    """Return the exact infrastructure and database scope of a VM deletion."""
    if PROVISIONING_BACKEND != "local_qemu":
        return None, "VM deletion requires the local QEMU backend"
    if request.get("status") not in ("completed", "failed"):
        return None, "Only completed VMs or failed requests can be deleted"

    hostname = request["hostname"]
    try:
        domain_exists = deletion_domain_exists(hostname)
    except (OSError, subprocess.SubprocessError, RuntimeError) as error:
        return None, f"Unable to verify VM state; nothing was deleted: {error}"
    failed = request.get("status") == "failed"
    if failed and domain_exists:
        return None, "A libvirt VM still exists for this failed request; administrator review is required"
    if failed and any(item["id"] != request["id"] and item.get("hostname") == hostname
                      for item in load_requests()):
        return None, "Another request uses this hostname; administrator review is required"
    storage_path = Path(LOCAL_QEMU["storage_path"]).resolve()
    candidate_paths = {
        storage_path / f"{hostname}.qcow2",
        storage_path / f"{hostname}.xml",
        storage_path / f"{hostname}-seed.img",
        storage_path / f"{hostname}-user-data.yml",
        storage_path / f"{hostname}-meta-data.yml",
    }
    for disk in request_data_disks(request):
        if disk.get("storage_file"):
            candidate_paths.add(storage_path / disk["storage_file"])

    def collect_stored_paths(value: object) -> None:
        if isinstance(value, dict):
            for nested in value.values():
                collect_stored_paths(nested)
        elif isinstance(value, list):
            for nested in value:
                collect_stored_paths(nested)
        elif isinstance(value, str) and value.startswith("/"):
            candidate_paths.add(Path(value))

    collect_stored_paths(request.get("manual_snapshots", []))
    collect_stored_paths(request.get("resize_snapshots", []))
    collect_stored_paths(request.get("snapshot_recovery_files", []))
    collect_stored_paths(request.get("manual_snapshot_restore_history", []))
    try:
        files = sorted(
            str(item.resolve())
            for item in candidate_paths
            if item.is_file() and item.resolve().parent == storage_path
        )
    except OSError as error:
        return None, f"Unable to inspect VM storage: {error}"

    with database() as connection:
        event_count = connection.execute(
            "SELECT COUNT(*) AS count FROM events WHERE request_id = ?",
            (request["id"],),
        ).fetchone()["count"]
    targets = ([f"libvirt-domain:{hostname}"] if domain_exists else []) + [
        f"file:{path}" for path in files
    ] + [
        f"database:events:{request['id']}:{event_count}",
        f"database:request:{request['id']}",
    ]
    return {
        "request_id": request["id"],
        "hostname": hostname,
        "domain_exists": domain_exists,
        "file_paths": files,
        "file_count": len(files),
        "failed_request": failed,
        "database_records": {"requests": 1, "events": event_count},
        "delete_targets": targets,
        "delete_target_count": len(targets),
        "warning": "This permanently removes the virtual machine, its files, and its portal database records.",
    }, ""


def delete_vm(request: dict, confirmed_targets: list[str], confirm_files: bool = False) -> tuple[bool, str]:
    """Delete a local-QEMU VM only when its freshly inspected scope matches."""
    plan, detail = vm_delete_plan(request)
    if not plan:
        return False, detail
    if confirmed_targets != plan["delete_targets"]:
        return False, "VM deletion scope changed; review and confirm the targets again"
    if plan["failed_request"] and plan["file_count"] and not confirm_files:
        return False, "Separate confirmation is required to remove leftover VM files"

    hostname = request["hostname"]
    uri = LOCAL_QEMU.get("uri", "qemu:///system")
    try:
        if plan["domain_exists"]:
            state = local_qemu_domain_state(hostname)
            if state != "shut off":
                result = subprocess.run(
                    ["virsh", "--connect", uri, "destroy", hostname],
                    capture_output=True, text=True, timeout=60, check=False,
                )
                if result.returncode:
                    raise RuntimeError((result.stderr or result.stdout).strip())
            result = subprocess.run(
                ["virsh", "--connect", uri, "undefine", hostname, "--nvram"],
                capture_output=True, text=True, timeout=60, check=False,
            )
            if result.returncode:
                # Domains without NVRAM reject --nvram; retry the plain undefine.
                result = subprocess.run(
                    ["virsh", "--connect", uri, "undefine", hostname],
                    capture_output=True, text=True, timeout=60, check=False,
                )
                if result.returncode:
                    raise RuntimeError((result.stderr or result.stdout).strip())
        for path in plan["file_paths"]:
            Path(path).unlink()
        with database() as connection:
            deleted = connection.execute(
                "DELETE FROM requests WHERE id = ?", (request["id"],)
            ).rowcount
            if deleted != 1:
                raise RuntimeError("VM request database record disappeared during deletion")
        return True, ""
    except (OSError, subprocess.SubprocessError, RuntimeError) as error:
        return False, str(error)


def guest_agent_responsive(hostname: str) -> bool:
    try:
        result = subprocess.run(
            [
                "virsh", "--connect", LOCAL_QEMU.get("uri", "qemu:///system"),
                "qemu-agent-command", hostname, '{"execute":"guest-ping"}',
            ],
            capture_output=True, text=True, timeout=10, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def create_resize_snapshot(request: dict, resources: dict, data_disks: list[dict]) -> tuple[dict | None, str]:
    hostname = request["hostname"]
    snapshot_id = f"resize-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:6]}"
    storage_path = Path(LOCAL_QEMU["storage_path"])
    xml_path = storage_path / f"{hostname}-{snapshot_id}.xml"
    try:
        xml_result = subprocess.run(
            ["virsh", "--connect", LOCAL_QEMU.get("uri", "qemu:///system"),
             "dumpxml", hostname, "--inactive"],
            capture_output=True, text=True, timeout=20, check=True,
        )
        xml_path.write_text(xml_result.stdout, encoding="utf-8")
        disks = domain_disk_files(hostname)
        if not disks:
            raise RuntimeError("No snapshot-capable VM disks were found")
        snapshot_disks = []
        command = [
            "virsh", "--connect", LOCAL_QEMU.get("uri", "qemu:///system"),
            "snapshot-create-as", hostname, snapshot_id,
            "--description", f"Automatic pre-resize snapshot for request {request['id']}",
            "--disk-only", "--atomic", "--no-metadata",
        ]
        for disk in disks:
            overlay = storage_path / f"{hostname}-{snapshot_id}-{disk['target']}.qcow2"
            command.extend([
                "--diskspec",
                f"{disk['target']},file={overlay},driver=qcow2",
            ])
            snapshot_disks.append({
                "target": disk["target"],
                "base": disk["source"],
                "overlay": str(overlay),
            })
        result = subprocess.run(
            command,
            capture_output=True, text=True, timeout=90, check=False,
        )
        if result.returncode:
            raise RuntimeError((result.stderr or result.stdout).strip())
        snapshot = {
            "id": snapshot_id,
            "created_at": now(),
            "expires_at": (datetime.now(UTC) + RESIZE_SNAPSHOT_TTL).isoformat(timespec="seconds"),
            "status": "active",
            "xml_path": str(xml_path),
            "disks": snapshot_disks,
            "previous_resources": dict(request["resources"]),
            "previous_additional_disks": [
                dict(disk) for disk in request_data_disks(request)
            ],
            "requested_resources": dict(resources),
            "requested_additional_disks": [dict(disk) for disk in data_disks],
        }
        for previous in request.get("resize_snapshots", []):
            if previous.get("status") == "active":
                previous["status"] = "superseded"
                previous["superseded_at"] = now()
        request.setdefault("resize_snapshots", []).append(snapshot)
        request["events"].append(event(
            "Pre-resize snapshot created",
            detail=f"{snapshot_id}; {len(snapshot_disks)} disk(s)",
        ))
        return snapshot, ""
    except (OSError, subprocess.SubprocessError, ET.ParseError, RuntimeError) as error:
        detail = str(error)
        request["events"].append(event("Pre-resize snapshot failed", "failed", detail))
        return None, detail


def create_manual_snapshot(
    request: dict, name: str, description: str, requested_by: str
) -> tuple[dict | None, str]:
    available = [
        item for item in request.get("manual_snapshots", [])
        if item.get("status") == "available"
    ]
    if len(available) >= 3:
        return None, "A maximum of 3 manual snapshots per VM is currently supported"
    hostname = request["hostname"]
    snapshot_id = f"manual-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:6]}"
    storage_path = Path(LOCAL_QEMU["storage_path"])
    xml_path = storage_path / f"{hostname}-{snapshot_id}.xml"
    state = local_qemu_domain_state(hostname)
    if state not in {"running", "shut off"}:
        return None, f"Snapshot requires a running or shut off VM; current state: {state}"
    try:
        xml_result = subprocess.run(
            [
                "virsh", "--connect", LOCAL_QEMU.get("uri", "qemu:///system"),
                "dumpxml", hostname, "--inactive",
            ],
            capture_output=True, text=True, timeout=20, check=True,
        )
        xml_path.write_text(xml_result.stdout, encoding="utf-8")
        disks = domain_disk_files(hostname)
        if not disks:
            raise RuntimeError("No snapshot-capable VM disks were found")
        snapshot_disks = []
        base_command = [
            "virsh", "--connect", LOCAL_QEMU.get("uri", "qemu:///system"),
            "snapshot-create-as", hostname, snapshot_id,
            "--description", description or name,
            "--disk-only", "--atomic", "--no-metadata",
        ]
        for disk in disks:
            overlay = storage_path / f"{hostname}-{snapshot_id}-{disk['target']}.qcow2"
            base_command.extend([
                "--diskspec",
                f"{disk['target']},file={overlay},driver=qcow2",
            ])
            snapshot_disks.append({
                "target": disk["target"],
                "base": disk["source"],
                "overlay": str(overlay),
            })
        quiesce = state == "running" and guest_agent_responsive(hostname)
        consistency = (
            "offline" if state == "shut off"
            else "application-consistent" if quiesce
            else "crash-consistent"
        )
        command = list(base_command)
        if quiesce:
            command.append("--quiesce")
        result = subprocess.run(
            command,
            capture_output=True, text=True, timeout=90, check=False,
        )
        if result.returncode:
            raise RuntimeError((result.stderr or result.stdout).strip())
        snapshot = {
            "id": snapshot_id,
            "name": name,
            "description": description,
            "created_at": now(),
            "created_by": requested_by,
            "status": "available",
            "consistency": consistency,
            "xml_path": str(xml_path),
            "disks": snapshot_disks,
            "resources": dict(request["resources"]),
            "additional_disks": [
                dict(disk) for disk in request_data_disks(request)
            ],
            "restores": [],
        }
        request.setdefault("manual_snapshots", []).append(snapshot)
        request["events"].append(event(
            "Manual snapshot created",
            detail=(
                f"{name} ({snapshot_id}); {len(snapshot_disks)} disk(s); "
                f"{consistency}; requested by {requested_by}"
            ),
        ))
        return snapshot, ""
    except (OSError, subprocess.SubprocessError, ET.ParseError, RuntimeError) as error:
        detail = str(error)
        request["events"].append(event("Manual snapshot failed", "failed", detail))
        return None, detail


def restore_manual_snapshot(
    request: dict, snapshot_id: str, requested_by: str
) -> tuple[bool, str]:
    snapshot = next(
        (
            item for item in request.get("manual_snapshots", [])
            if item.get("id") == snapshot_id and item.get("status") == "available"
        ),
        None,
    )
    if not snapshot:
        return False, "Available manual snapshot not found"
    state = local_qemu_domain_state(request["hostname"])
    if state != "shut off":
        return False, f"Snapshot restore requires a shut off VM; current state: {state}"
    restore_id = f"restore-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:6]}"
    storage_path = Path(LOCAL_QEMU["storage_path"])
    restore_xml_path = storage_path / f"{request['hostname']}-{restore_id}.xml"
    expected_overlays = []
    previous_xml = ""
    domain_redefined = False
    try:
        current_xml_result = subprocess.run(
            [
                "virsh", "--connect", LOCAL_QEMU.get("uri", "qemu:///system"),
                "dumpxml", request["hostname"], "--inactive",
            ],
            capture_output=True, text=True, timeout=20, check=False,
        )
        if current_xml_result.returncode:
            raise RuntimeError(
                (current_xml_result.stderr or current_xml_result.stdout).strip()
                or "Unable to save the current domain definition"
            )
        previous_xml = current_xml_result.stdout
        xml_root = ET.parse(snapshot["xml_path"]).getroot()
        disk_sources = {
            disk.find("target").get("dev"): disk.find("source")
            for disk in xml_root.findall("./devices/disk[@device='disk']")
            if disk.find("target") is not None and disk.find("source") is not None
        }
        snapshot_command = [
            "virsh", "--connect", LOCAL_QEMU.get("uri", "qemu:///system"),
            "snapshot-create-as", request["hostname"], restore_id,
            "--description", f"Restore of manual snapshot {snapshot_id}",
            "--disk-only", "--atomic", "--no-metadata",
        ]
        for disk in snapshot["disks"]:
            base = Path(disk["base"])
            if not base.exists():
                raise RuntimeError(f"Snapshot base disk is missing: {base}")
            source = disk_sources.get(disk["target"])
            if source is None:
                raise RuntimeError(f"Disk {disk['target']} is missing from saved domain XML")
            source.set("file", str(base))
            overlay = storage_path / (
                f"{request['hostname']}-{restore_id}-{disk['target']}.qcow2"
            )
            expected_overlays.append({
                "target": disk["target"],
                "base": str(base),
                "overlay": str(overlay),
            })
            snapshot_command.extend([
                "--diskspec",
                f"{disk['target']},file={overlay},driver=qcow2",
            ])
        if not expected_overlays:
            raise RuntimeError("Snapshot contains no restorable disks")
        snapshot_base_xml = ET.tostring(
            xml_root, encoding="unicode", xml_declaration=True
        )
        result = subprocess.run(
            [
                "virsh", "--connect", LOCAL_QEMU.get("uri", "qemu:///system"),
                "define", "/dev/stdin",
            ],
            input=snapshot_base_xml,
            capture_output=True, text=True, timeout=30, check=False,
        )
        if result.returncode:
            raise RuntimeError((result.stderr or result.stdout).strip())
        domain_redefined = True
        result = subprocess.run(
            snapshot_command,
            capture_output=True, text=True, timeout=90, check=False,
        )
        if result.returncode:
            raise RuntimeError(
                (result.stderr or result.stdout).strip()
                or "Libvirt snapshot restore failed"
            )
        restored_sources = {
            disk["target"]: disk["source"]
            for disk in domain_disk_files(request["hostname"])
        }
        for disk in expected_overlays:
            if restored_sources.get(disk["target"]) != disk["overlay"]:
                raise RuntimeError(
                    f"Restore verification failed for disk {disk['target']}: "
                    f"expected {disk['overlay']}, got "
                    f"{restored_sources.get(disk['target'], 'no disk')}"
                )
            disk_sources[disk["target"]].set("file", disk["overlay"])
        ET.ElementTree(xml_root).write(
            restore_xml_path, encoding="unicode", xml_declaration=True
        )
        request["resources"] = dict(snapshot["resources"])
        request["additional_disks"] = [
            dict(disk) for disk in snapshot["additional_disks"]
        ]
        request.pop("additional_disk", None)
        snapshot.setdefault("restores", []).append({
            "id": restore_id,
            "restored_at": now(),
            "restored_by": requested_by,
            "xml_path": str(restore_xml_path),
            "overlays": expected_overlays,
        })
        request.pop("last_failed_resize", None)
        request["events"].append(event(
            "Manual snapshot restored",
            detail=(
                f"{snapshot['name']} ({snapshot_id}); restore branch {restore_id}; "
                f"requested by {requested_by}"
            ),
        ))
        return True, ""
    except (OSError, subprocess.SubprocessError, ET.ParseError, RuntimeError) as error:
        detail = str(error)
        if domain_redefined and previous_xml:
            rollback_result = subprocess.run(
                [
                    "virsh", "--connect", LOCAL_QEMU.get("uri", "qemu:///system"),
                    "define", "/dev/stdin",
                ],
                input=previous_xml,
                capture_output=True, text=True, timeout=30, check=False,
            )
            if rollback_result.returncode:
                rollback_detail = (
                    rollback_result.stderr or rollback_result.stdout
                ).strip()
                detail = (
                    f"{detail}; additionally failed to restore the previous domain "
                    f"definition: {rollback_detail}"
                )
        recovery_files = [
            disk for disk in expected_overlays if Path(disk["overlay"]).exists()
        ]
        if recovery_files:
            request.setdefault("snapshot_recovery_files", []).extend(recovery_files)
        request["events"].append(event("Manual snapshot restore failed", "failed", detail))
        return False, detail


def manual_snapshot_delete_plan(request: dict, snapshot_id: str) -> tuple[dict | None, str]:
    snapshot = next(
        (
            item for item in request.get("manual_snapshots", [])
            if item.get("id") == snapshot_id and item.get("status") == "available"
        ),
        None,
    )
    if not snapshot:
        return None, "Available manual snapshot not found"
    xml_path = Path(snapshot["xml_path"])
    targets = [str(xml_path)] if xml_path.is_file() else []
    retained = sorted({
        disk["overlay"]
        for disk in snapshot.get("disks", [])
        if disk.get("overlay")
    })
    for restore in snapshot.get("restores", []):
        retained.extend(
            disk["overlay"]
            for disk in restore.get("overlays", [])
            if disk.get("overlay")
        )
    return {
        "snapshot_id": snapshot["id"],
        "snapshot_name": snapshot["name"],
        "delete_targets": targets,
        "delete_target_count": len(targets),
        "retained_disk_files": sorted(set(retained)),
        "warning": (
            "Snapshot restore metadata will be removed. QCOW2 disk-chain files are retained "
            "to avoid breaking the active VM or another snapshot branch."
        ),
    }, ""


def delete_manual_snapshot(
    request: dict, snapshot_id: str, requested_by: str, confirmed_targets: list[str]
) -> tuple[bool, str]:
    plan, detail = manual_snapshot_delete_plan(request, snapshot_id)
    if not plan:
        return False, detail
    if confirmed_targets != plan["delete_targets"]:
        return False, "Snapshot deletion scope changed; review and confirm the targets again"
    snapshot = next(
        item for item in request["manual_snapshots"] if item.get("id") == snapshot_id
    )
    try:
        for target in plan["delete_targets"]:
            Path(target).unlink()
        snapshot["status"] = "deleted"
        snapshot["deleted_at"] = now()
        snapshot["deleted_by"] = requested_by
        snapshot["deleted_files"] = list(plan["delete_targets"])
        snapshot["retained_disk_files"] = list(plan["retained_disk_files"])
        request["events"].append(event(
            "Manual snapshot deleted",
            detail=(
                f"{snapshot['name']} ({snapshot_id}); requested by {requested_by}; "
                f"{plan['delete_target_count']} metadata file(s) deleted; "
                f"{len(plan['retained_disk_files'])} qcow2 recovery file(s) retained"
            ),
        ))
        return True, ""
    except OSError as error:
        detail = str(error)
        request["events"].append(event("Manual snapshot deletion failed", "failed", detail))
        return False, detail


def expire_resize_snapshots(requests: list[dict], current_time: datetime | None = None) -> bool:
    current_time = current_time or datetime.now(UTC)
    changed = False
    for request in requests:
        for snapshot in request.get("resize_snapshots", []):
            if snapshot.get("status") != "active":
                continue
            try:
                expires_at = datetime.fromisoformat(
                    snapshot.get("expires_at")
                    or (
                        datetime.fromisoformat(snapshot["created_at"]) + RESIZE_SNAPSHOT_TTL
                    ).isoformat()
                )
            except (KeyError, TypeError, ValueError):
                continue
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=UTC)
            if not snapshot.get("expires_at"):
                snapshot["expires_at"] = expires_at.isoformat(timespec="seconds")
                changed = True
            if current_time >= expires_at:
                snapshot["status"] = "expired"
                snapshot["expired_at"] = current_time.isoformat(timespec="seconds")
                request["events"].append(event(
                    "Resize snapshot expired",
                    detail=f"{snapshot['id']}; rollback disabled; cleanup confirmation required",
                ))
                changed = True
    return changed


def resize_snapshot_cleanup_plan(
    request: dict, snapshot_id: str
) -> tuple[dict | None, str]:
    snapshot = next(
        (
            item for item in request.get("resize_snapshots", [])
            if item.get("id") == snapshot_id and item.get("status") == "expired"
        ),
        None,
    )
    if not snapshot:
        return None, "Expired resize snapshot not found"
    current_disks = {
        item["target"]: item["source"] for item in domain_disk_files(request["hostname"])
    }
    pending_commit = []
    for disk in snapshot.get("disks", []):
        current = current_disks.get(disk["target"])
        if current == disk["overlay"]:
            pending_commit.append(disk["target"])
        elif current != disk["base"]:
            return None, (
                f"Disk {disk['target']} no longer points to the expected snapshot overlay "
                "or base; automatic cleanup is unsafe"
            )
    targets = [
        path for path in (
            [disk.get("overlay") for disk in snapshot.get("disks", [])]
            + [snapshot.get("xml_path")]
        )
        if path and Path(path).is_file()
    ]
    return {
        "snapshot_id": snapshot["id"],
        "delete_targets": targets,
        "delete_target_count": len(targets),
        "pending_commit_disks": pending_commit,
        "warning": (
            "Current overlay data will first be committed into the base disks and pivoted. "
            "Only after disk-chain verification will the listed files be deleted."
        ),
    }, ""


def cleanup_expired_resize_snapshot(
    request: dict,
    snapshot_id: str,
    requested_by: str,
    confirmed_targets: list[str],
) -> tuple[bool, str]:
    plan, detail = resize_snapshot_cleanup_plan(request, snapshot_id)
    if not plan:
        return False, detail
    if confirmed_targets != plan["delete_targets"]:
        return False, "Snapshot cleanup scope changed; review and confirm the targets again"
    if local_qemu_domain_state(request["hostname"]) != "running":
        return False, "Resize snapshot cleanup requires a running VM"
    snapshot = next(
        item for item in request["resize_snapshots"] if item.get("id") == snapshot_id
    )
    try:
        for target in plan["pending_commit_disks"]:
            result = subprocess.run(
                [
                    "virsh", "--connect", LOCAL_QEMU.get("uri", "qemu:///system"),
                    "blockcommit", request["hostname"], target,
                    "--active", "--pivot", "--wait",
                ],
                capture_output=True, text=True, timeout=3600, check=False,
            )
            if result.returncode:
                raise RuntimeError((result.stderr or result.stdout).strip())
        verified = {
            item["target"]: item["source"] for item in domain_disk_files(request["hostname"])
        }
        for disk in snapshot.get("disks", []):
            if verified.get(disk["target"]) != disk["base"]:
                raise RuntimeError(
                    f"Disk-chain verification failed for {disk['target']}; files retained"
                )
        for target in plan["delete_targets"]:
            Path(target).unlink()
        snapshot["status"] = "cleaned"
        snapshot["cleaned_at"] = now()
        snapshot["cleaned_by"] = requested_by
        snapshot["deleted_files"] = list(plan["delete_targets"])
        request["events"].append(event(
            "Expired resize snapshot cleaned",
            detail=(
                f"{snapshot_id}; requested by {requested_by}; "
                f"{len(plan['pending_commit_disks'])} disk(s) committed; "
                f"{plan['delete_target_count']} file(s) deleted"
            ),
        ))
        return True, ""
    except (OSError, subprocess.SubprocessError, RuntimeError) as error:
        detail = str(error)
        request["events"].append(event(
            "Expired resize snapshot cleanup failed", "failed", detail
        ))
        return False, detail


def rollback_resize_snapshot(request: dict, snapshot_id: str, requested_by: str) -> tuple[bool, str]:
    snapshot = next(
        (item for item in request.get("resize_snapshots", []) if item.get("id") == snapshot_id),
        None,
    )
    if not snapshot or snapshot.get("status") != "active":
        return False, "Active snapshot not found"
    state = local_qemu_domain_state(request["hostname"])
    if state != "shut off":
        return False, f"Rollback requires a shut off VM; current state: {state}"
    recovery_suffix = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    moved = []
    try:
        for disk in snapshot["disks"]:
            overlay = Path(disk["overlay"])
            if not overlay.exists():
                raise RuntimeError(f"Snapshot overlay is missing: {overlay}")
            recovery = overlay.with_name(f"{overlay.name}.rolled-back-{recovery_suffix}")
            overlay.rename(recovery)
            moved.append({"from": str(overlay), "to": str(recovery)})
        result = subprocess.run(
            ["virsh", "--connect", LOCAL_QEMU.get("uri", "qemu:///system"),
             "define", snapshot["xml_path"]],
            capture_output=True, text=True, timeout=30, check=False,
        )
        if result.returncode:
            raise RuntimeError((result.stderr or result.stdout).strip())
        request["resources"] = dict(snapshot["previous_resources"])
        request["additional_disks"] = [
            dict(disk) for disk in snapshot["previous_additional_disks"]
        ]
        snapshot["status"] = "rolled_back"
        snapshot["rolled_back_at"] = now()
        snapshot["rolled_back_by"] = requested_by
        snapshot["recovery_files"] = moved
        request.pop("last_failed_resize", None)
        request["events"].append(event(
            "Resize snapshot rolled back",
            detail=f"{snapshot_id}; requested by {requested_by}; overlay files retained for recovery",
        ))
        return True, ""
    except (OSError, subprocess.SubprocessError, RuntimeError) as error:
        for item in reversed(moved):
            try:
                Path(item["to"]).rename(item["from"])
            except OSError:
                pass
        detail = str(error)
        request["events"].append(event("Resize rollback failed", "failed", detail))
        return False, detail


def request_blocks_hostname(item: dict, hostname: str) -> bool:
    """Reserve names for active requests and completed VMs that still exist."""
    if item.get("hostname") != hostname:
        return False
    status = item.get("status")
    if status in {"awaiting_approval", "resize_awaiting_approval", "provisioning"}:
        return True
    if status != "completed":
        return False
    if PROVISIONING_BACKEND == "local_qemu":
        return local_qemu_domain_exists(hostname)
    return True


def preflight(hostname: str, resources: dict, image: str) -> tuple[dict | None, list[str]]:
    if PROVISIONING_BACKEND == "local_qemu":
        return preflight_local_qemu(hostname, resources, image)
    return preflight_vcsim(hostname, resources)


def request_data_disks(request: dict) -> list[dict]:
    """Return the current list while remaining compatible with old single-disk records."""
    if isinstance(request.get("additional_disks"), list):
        return request["additional_disks"]
    old_disk = request.get("additional_disk")
    if old_disk:
        return [{
            **old_disk,
            "target": "vdb",
            "storage_file": f"{request['hostname']}-data.qcow2",
            "index": 1,
        }]
    return []


def validate_resize(
    payload: dict, request: dict
) -> tuple[dict | None, list[dict] | None, list[str] | None, list[str]]:
    errors: list[str] = []
    approval_reasons: list[str] = []
    resources: dict[str, int] = {}
    for key, label in (("vcpu", "vCPU"), ("memory_gb", "Memory"), ("disk_gb", "Disk")):
        try:
            value = int(payload.get(key, 0))
            current_value = int(request["resources"][key])
            self_service_limit = int(POLICY["automatic"][key])
            if value < current_value:
                errors.append(f"{label} cannot be reduced during an online resize.")
            elif value > int(POLICY["hard"][key]):
                errors.append(f"{label} exceeds the hard limit of {POLICY['hard'][key]}.")
            elif value > max(current_value, self_service_limit):
                approval_reasons.append(
                    f"{label} exceeds the self-service resize limit "
                    f"({value} requested, {self_service_limit} limit)"
                )
            resources[key] = value
        except (TypeError, ValueError):
            errors.append(f"{label} must be a positive whole number.")
    current_disks = request_data_disks(request)
    supplied_disks = payload.get("additional_disks", [])
    resized_disks: list[dict] = []
    if not isinstance(supplied_disks, list) or len(supplied_disks) != len(current_disks):
        errors.append("Every portal-managed data disk must be included in the resize request.")
    else:
        for current, supplied in zip(current_disks, supplied_disks):
            try:
                size_gb = int(supplied.get("size_gb", 0))
                current_size_gb = int(current["size_gb"])
                self_service_limit = int(POLICY["automatic"]["disk_gb"])
                if size_gb < current_size_gb:
                    errors.append(f"Data disk at {current['mountpoint']} cannot be reduced.")
                elif size_gb > MAX_ADDITIONAL_DISK_GB:
                    errors.append(
                        f"Data disk at {current['mountpoint']} exceeds {MAX_ADDITIONAL_DISK_GB} GB."
                    )
                elif size_gb > max(current_size_gb, self_service_limit):
                    approval_reasons.append(
                        f"Data disk at {current['mountpoint']} exceeds the self-service "
                        f"resize limit ({size_gb} GB requested, {self_service_limit} GB limit)"
                    )
                resized_disks.append({**current, "size_gb": size_gb})
            except (AttributeError, TypeError, ValueError):
                errors.append(f"Data disk size at {current['mountpoint']} must be a whole number.")
    # Repeating the current sizes reconciles a previous partial hot-add:
    # libvirt may already have the requested CPUs while Linux leaves them offline.
    if errors:
        return None, None, None, errors
    return resources, resized_disks, approval_reasons, []


def preflight_local_resize(
    request: dict,
    resources: dict,
    data_disks: list[dict],
    *,
    retain_reserve: bool = True,
) -> list[str]:
    additional_gb = resources["disk_gb"] - int(request["resources"]["disk_gb"])
    additional_gb += sum(
        new["size_gb"] - old["size_gb"]
        for new, old in zip(data_disks, request_data_disks(request))
    )
    if additional_gb <= 0:
        return []
    storage_path = Path(LOCAL_QEMU["storage_path"])
    try:
        filesystem = os.statvfs(storage_path)
        capacity = filesystem.f_blocks * filesystem.f_frsize
        free = filesystem.f_bavail * filesystem.f_frsize
    except OSError:
        return [PUBLIC_PREFLIGHT_ERROR]
    reserve_percent = int(LOCAL_QEMU.get("reserve_percent", DATASTORE_RESERVE_PERCENT))
    reserve = capacity * reserve_percent // 100
    usable_free = max(0, free - reserve) if retain_reserve else free
    if additional_gb * GIB > usable_free:
        if not retain_reserve:
            return [
                f"Insufficient local QEMU storage for approved resize: "
                f"{additional_gb} GB additional requested, {free // GIB} GB free."
            ]
        return [PUBLIC_PREFLIGHT_ERROR]
    return []


def resize_local_qemu(
    request: dict,
    resources: dict,
    data_disks: list[dict],
    requested_by: str,
    *,
    create_snapshot: bool = True,
) -> tuple[bool, str]:
    previous_resources = dict(request["resources"])
    old_data_disks = request_data_disks(request)
    previous_disks = [dict(disk) for disk in old_data_disks]
    snapshot = None
    if create_snapshot:
        snapshot, detail = create_resize_snapshot(request, resources, data_disks)
        if not snapshot:
            request["last_failed_resize"] = {
                "resources": dict(resources),
                "additional_disks": [dict(disk) for disk in data_disks],
                "requested_by": requested_by,
                "failed_at": now(),
                "detail": detail,
            }
            send_notification("failed", request, detail=f"Pre-resize snapshot failed: {detail}")
            return False, detail
    resize_data_disks = [
        {**new, "resize": new["size_gb"] > old["size_gb"]}
        for new, old in zip(data_disks, old_data_disks)
    ]
    extra_vars = {
        "vm_name": request["hostname"],
        "vm_vcpu": resources["vcpu"],
        "vm_memory_mb": resources["memory_gb"] * 1024,
        "vm_disk_gb": resources["disk_gb"],
        "resize_guest_disk": resources["disk_gb"] > int(request["resources"]["disk_gb"]),
        "data_disks": resize_data_disks,
        "storage_path": LOCAL_QEMU["storage_path"],
        "libvirt_uri": LOCAL_QEMU.get("uri", "qemu:///system"),
    }
    request["events"].append(event(
        "Online resize started",
        "waiting",
        (f"Requested by {requested_by}: {resources['vcpu']} vCPU, "
         f"{resources['memory_gb']} GB RAM, {resources['disk_gb']} GB OS disk"),
    ))
    try:
        result = subprocess.run(
            ["ansible-playbook", "/app/ansible/resize-local-qemu.yml", "-e", json.dumps(extra_vars)],
            capture_output=True, text=True, timeout=180, check=False,
        )
        output = (result.stdout + "\n" + result.stderr).strip()
        request["resize_ansible_output"] = output[-12000:]
        if result.returncode:
            detail = output[-1500:] or "Ansible returned an error"
            request["events"].append(event("Online resize failed", "failed", detail))
            request["last_failed_resize"] = {
                "resources": dict(resources),
                "additional_disks": [dict(disk) for disk in data_disks],
                "requested_by": requested_by,
                "failed_at": now(),
                "detail": detail,
                "snapshot_id": snapshot["id"] if snapshot else None,
            }
            send_notification("failed", request, detail=detail)
            return False, detail
        request["resources"] = resources
        request["additional_disks"] = data_disks
        request.pop("additional_disk", None)
        request["events"].append(event(
            "Online resize completed",
            detail=(f"{resources['vcpu']} vCPU, {resources['memory_gb']} GB RAM, "
                    f"{resources['disk_gb']} GB OS disk, {len(data_disks)} data disk(s); no reboot"),
        ))
        request.pop("last_failed_resize", None)
        if snapshot:
            snapshot["resize_completed_at"] = now()
        # All successful resize paths (direct, approved and retried) converge here.
        # Sync only after replacing both CPU/RAM/OS-disk and data-disk values.
        # Inventory failure is recorded separately and does not undo the resize.
        register_netbox(request)
        send_notification(
            "resized",
            request,
            previous_resources=previous_resources,
            previous_disks=previous_disks,
        )
        return True, ""
    except (OSError, subprocess.TimeoutExpired) as error:
        request["events"].append(event("Online resize failed", "failed", str(error)))
        request["last_failed_resize"] = {
            "resources": dict(resources),
            "additional_disks": [dict(disk) for disk in data_disks],
            "requested_by": requested_by,
            "failed_at": now(),
            "detail": str(error),
            "snapshot_id": snapshot["id"] if snapshot else None,
        }
        send_notification("failed", request, detail=str(error))
        return False, str(error)


def log_preflight(hostname: str, disk_gb: int, result: str, detail: str) -> None:
    with database() as connection:
        connection.execute(
            "INSERT INTO preflight_checks(timestamp, hostname, requested_disk_gb, result, detail) VALUES (?, ?, ?, ?, ?)",
            (now(), hostname, disk_gb, result, detail),
        )


def validate(payload: dict, authenticated_user: str) -> tuple[dict | None, list[str]]:
    errors: list[str] = []
    hostname = str(payload.get("hostname", "")).strip().lower()
    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", hostname):
        errors.append("Hostname must contain only lowercase letters, numbers and hyphens.")
    fqdn = str(payload.get("fqdn", "")).strip().lower().rstrip(".")
    fqdn_pattern = (
        r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
        r"(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+"
    )
    if len(fqdn) > 253 or not re.fullmatch(fqdn_pattern, fqdn):
        errors.append("Full DNS name must be valid, for example app-dev-01.lab.example.ee.")
    elif not fqdn.startswith(f"{hostname}."):
        errors.append(f"Full DNS name must begin with the VM name: {hostname}.*")
    domain = fqdn[len(hostname) + 1:] if fqdn.startswith(f"{hostname}.") else ""

    environment = payload.get("environment")
    if environment not in ENVIRONMENTS:
        errors.append("Unknown environment.")
    if payload.get("image") not in IMAGES:
        errors.append("Unknown operating system image.")
    if payload.get("network") not in NETWORKS_BY_PORTGROUP:
        errors.append("Unknown network.")

    selected_key = None
    selected_key_id = str(payload.get("ssh_key_id", "")).strip()
    keys = user_ssh_keys(authenticated_user)
    if not keys:
        errors.append("Add an SSH public key in Settings before requesting a VM.")
    else:
        selected_key = next((key for key in keys if key["id"] == selected_key_id), None)
        if not selected_key:
            errors.append("Select one of your SSH public keys.")

    profile = payload.get("profile")
    if profile in PROFILES:
        resources = {key: PROFILES[profile][key] for key in ("vcpu", "memory_gb", "disk_gb")}
    elif profile == "custom":
        resources = {}
        for key, label in (("vcpu", "vCPU"), ("memory_gb", "Memory"), ("disk_gb", "Disk")):
            try:
                value = int(payload.get(key, 0))
                if value < 1:
                    raise ValueError
                resources[key] = value
            except (TypeError, ValueError):
                errors.append(f"{label} must be a positive whole number.")
    else:
        resources = {}
        errors.append("Unknown resource profile.")

    for key, value in resources.items():
        if value > POLICY["hard"][key]:
            errors.append(f"{key} exceeds the hard limit of {POLICY['hard'][key]}.")

    additional_disks = []
    supplied_disks = payload.get("additional_disks", [])
    if not isinstance(supplied_disks, list):
        errors.append("Additional disks must be a list.")
        supplied_disks = []
    if len(supplied_disks) > MAX_ADDITIONAL_DISKS:
        errors.append(f"A maximum of {MAX_ADDITIONAL_DISKS} additional disks is supported.")
    seen_mountpoints = set()
    for index, supplied_disk in enumerate(supplied_disks, start=1):
        try:
            size_gb = int(supplied_disk.get("size_gb", 0))
            if not 1 <= size_gb <= MAX_ADDITIONAL_DISK_GB:
                raise ValueError
        except (AttributeError, TypeError, ValueError):
            errors.append(f"Additional disk must be between 1 and {MAX_ADDITIONAL_DISK_GB} GB.")
            size_gb = 0
        mountpoint = str(supplied_disk.get("mountpoint", "")).strip()
        if not re.fullmatch(r"/(?:[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*)?", mountpoint):
            errors.append("Additional disk mountpoint must be an absolute path without spaces.")
        elif mountpoint in {"/", "/boot", "/boot/efi", "/dev", "/proc", "/sys", "/run"}:
            errors.append("This mountpoint is reserved by the operating system.")
        elif mountpoint in seen_mountpoints:
            errors.append(f"Additional disk mountpoint is duplicated: {mountpoint}.")
        seen_mountpoints.add(mountpoint)
        target = f"vd{chr(ord('a') + index)}"
        additional_disks.append({
            "index": index,
            "size_gb": size_gb,
            "mountpoint": mountpoint,
            "filesystem": "ext4",
            "target": target,
            "storage_file": f"{hostname}-data-{index}.qcow2",
            "vg_name": f"vg_data_{index}",
            "lv_name": "lv_data",
        })

    owner = str(payload.get("owner", "")).strip()
    project = str(payload.get("project", "")).strip()
    if not owner:
        errors.append("Owner is required.")
    if not project:
        errors.append("Project is required.")
    if errors:
        return None, errors

    approval_reasons = [
        f"{key} exceeds automatic limit ({POLICY['automatic'][key]})"
        for key in ("vcpu", "memory_gb")
        if resources[key] > POLICY["automatic"][key]
    ]
    disk_limit_gb = POLICY["automatic"]["disk_gb"]
    if resources["disk_gb"] > disk_limit_gb:
        approval_reasons.append(
            f"OS disk exceeds automatic limit "
            f"({resources['disk_gb']} GB requested, {disk_limit_gb} GB limit)"
        )
    approval_reasons.extend(
        f"Data disk at {disk['mountpoint']} exceeds automatic limit "
        f"({disk['size_gb']} GB requested, {disk_limit_gb} GB limit)"
        for disk in additional_disks
        if disk["size_gb"] > disk_limit_gb
    )
    if ENVIRONMENTS[environment]["approval"]:
        approval_reasons.append("Production environment always requires approval")

    clean = {
        "hostname": hostname,
        "fqdn": fqdn,
        "domain": domain,
        "environment": environment,
        "image": payload["image"],
        "network": payload["network"],
        "network_label": (
            f"{NETWORKS_BY_PORTGROUP[payload['network']]['cidr']} "
            f"({NETWORKS_BY_PORTGROUP[payload['network']]['description']})"
        ),
        "profile": profile,
        "resources": resources,
        "additional_disks": additional_disks,
        "owner": owner,
        "project": project,
        "purpose": str(payload.get("purpose", "")).strip(),
        "approval_reasons": approval_reasons,
        "ssh_key": selected_key,
        "ssh_login_user": selected_key["login_user"] if selected_key else SSH_LOGIN_USER,
    }
    return clean, []


def local_qemu_ip(hostname: str, timeout_seconds: int = 20) -> str | None:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        result = subprocess.run(
            ["virsh", "--connect", LOCAL_QEMU.get("uri", "qemu:///system"),
             "domifaddr", hostname, "--source", "lease"],
            capture_output=True, text=True, timeout=10, check=False,
        )
        if result.returncode == 0:
            match = re.search(r"\b(?:ipv4|ipv6)\s+([^\s/]+)/\d+", result.stdout)
            if match:
                return match.group(1)
        time.sleep(1)
    return None


def register_netbox(request: dict) -> bool:
    """Inventory failure must not invalidate a successfully provisioned VM."""
    if not NETBOX_ENABLED:
        request['netbox'] = {'status': 'disabled'}
        return False
    registration_event = next((item for item in request['events']
                               if item['name'] == 'NetBox registration'), None)
    if registration_event is None:
        registration_event = event('NetBox registration')
        request['events'].append(registration_event)
    try:
        result = NetBoxClient(NETBOX_URL, NETBOX_TOKEN, NETBOX_CLUSTER_ID).register(
            request, request_data_disks(request))
        request['netbox'] = dict(result, status='registered', synced_at=now())
        registration_event.update(state='done', timestamp=now(), detail=f'VM registered in NetBox (ID {result["vm_id"]})')
        return True
    except (NetBoxError, ValueError, KeyError, TypeError) as error:
        # Keep error messages credential-free and leave the VM's status intact.
        detail = str(error) if isinstance(error, NetBoxError) else 'Invalid NetBox configuration or response'
        request['netbox'] = {'status': 'error', 'error': detail, 'attempted_at': now()}
        registration_event.update(state='failed', timestamp=now(), detail=detail)
        return False


def reconcile_pending_ip(requests: list[dict]) -> None:
    """Resolve DHCP leases that appeared after the initial provisioning wait."""
    if PROVISIONING_BACKEND != "local_qemu":
        return
    for request in requests:
        pending_event = next((
            item for item in request.get("events", [])
            if item.get("name") == "IP address pending"
        ), None)
        if request.get("status") != "completed" or not pending_event:
            continue
        ip_address = local_qemu_ip(request["hostname"], timeout_seconds=2)
        if not ip_address:
            continue
        request["ip_address"] = ip_address
        pending_event.update(
            name="IP address assigned",
            state="done",
            detail=ip_address,
            timestamp=now(),
        )
        if request.get('netbox', {}).get('status') in ('registered', 'error'):
            register_netbox(request)
        save_requests([request])


def provision(request: dict) -> None:
    request["status"] = "provisioning"
    storage_resources = dict(request["resources"])
    storage_resources["disk_gb"] += sum(
        disk["size_gb"] for disk in request_data_disks(request)
    )
    placement, errors = preflight(request["hostname"], storage_resources, request["image"])
    if errors:
        request["status"] = "failed"
        detail = "; ".join(errors)
        request["events"].append(event("Infrastructure preflight failed", "failed", detail))
        send_notification("failed", request, detail=detail)
        return
    request["placement"] = placement
    if PROVISIONING_BACKEND != "local_qemu":
        request["events"].append(event("IP address selected", detail="PoC simulation: 192.0.2.42"))
    request["events"].append(event(
        "Infrastructure placement revalidated",
        detail=(f"{placement['datacenter']} / {placement['datastore']}; "
                f"{placement['free_before_gb']} GB free before, {placement['free_after_gb']} GB after"),
    ))
    extra_vars = {
        "vm_name": request["hostname"],
        "vm_vcpu": request["resources"]["vcpu"],
        "vm_memory_mb": request["resources"]["memory_gb"] * 1024,
        "vm_disk_gb": request["resources"]["disk_gb"],
        "additional_disks": request_data_disks(request),
        "ssh_public_key": request["ssh_key"]["public_key"],
        "ssh_login_user": request.get("ssh_login_user", SSH_LOGIN_USER),
        "datastore": placement["datastore"],
        "resource_pool": placement["resource_pool"],
        "network_name": request["network"],
    }
    playbook = "/app/ansible/provision-vm.yml"
    if PROVISIONING_BACKEND == "local_qemu":
        playbook = "/app/ansible/provision-local-qemu.yml"
        extra_vars.update({
            "base_image": placement["base_image"],
            "storage_path": placement["storage_path"],
            "libvirt_uri": LOCAL_QEMU.get("uri", "qemu:///system"),
            "libvirt_network": LOCAL_QEMU.get("network", "default"),
            "cloud_init_template": LOCAL_QEMU["cloud_init_template"],
            "os_info": {
                "Ubuntu 20.04 LTS": "ubuntu20.04",
                "Ubuntu 22.04 LTS": "ubuntu22.04",
                "Ubuntu 24.04 LTS": "ubuntu24.04",
                "Ubuntu 24.04 LTS (LVM)": "ubuntu24.04",
            }.get(request["image"], "detect=on,require=off"),
        })
    try:
        result = subprocess.run(
            ["ansible-playbook", playbook, "-e", json.dumps(extra_vars)],
            capture_output=True, text=True, timeout=120, check=False,
        )
        output = (result.stdout + "\n" + result.stderr).strip()
        request["ansible_output"] = output[-12000:]
        if result.returncode:
            request["status"] = "failed"
            detail = output[-1000:] or "Ansible returned an error"
            request["events"].append(event("Virtual machine creation failed", "failed", detail))
            send_notification("failed", request, detail=detail)
            return
        request["events"].append(event("Virtual machine created", detail=f"Ansible completed successfully using {PROVISIONING_BACKEND}"))
        request["events"].append(event("Guest configuration recorded", detail="Requested CPU, RAM and disk applied"))
        if PROVISIONING_BACKEND == "local_qemu":
            ip_address = local_qemu_ip(request["hostname"])
            if ip_address:
                request["ip_address"] = ip_address
                request["events"].append(event("IP address assigned", detail=ip_address))
            else:
                request["events"].append(event("IP address pending", "waiting", "DHCP lease is not available yet"))
        register_netbox(request)
        request["events"].append(event("Nagios registration", detail="PoC simulation"))
        simulate_hostmaster_email(request)
        request["status"] = "completed"
        send_notification("created", request)
    except (OSError, subprocess.TimeoutExpired) as error:
        request["status"] = "failed"
        request["events"].append(event("Ansible execution failed", "failed", str(error)))
        send_notification("failed", request, detail=str(error))


class PortalHandler(SimpleHTTPRequestHandler):
    # WebSocket upgrades require an HTTP/1.1 status line.  The stdlib
    # handler defaults to HTTP/1.0, which Chromium rejects even when the
    # response otherwise contains a valid 101 upgrade handshake.
    protocol_version = "HTTP/1.1"

    # After a WebSocket upgrade the proxy reads frames directly from the
    # underlying socket.  Disable the HTTP request reader's read-ahead buffer
    # so it cannot swallow the first SPICE frame when Chromium sends it
    # immediately after the upgrade request.
    rbufsize = 0

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(STATIC_DIR), **kwargs)

    def log_message(self, format: str, *args) -> None:
        print(f"{self.address_string()} - {format % args}")

    def send_json(self, body: object, status: HTTPStatus = HTTPStatus.OK) -> None:
        encoded = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(encoded)

    def read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        return json.loads(self.rfile.read(length) or b"{}")

    def current_user(self) -> str | None:
        session = self.current_session()
        return session["username"] if session else None

    def current_session(self) -> dict | None:
        cookies = self.headers.get("Cookie", "")
        token = next((part.split("=", 1)[1] for part in cookies.split("; ") if part.startswith("vmportal_session=")), None)
        if not token:
            return None
        session = SESSIONS.get(token)
        if not session or session["expires"] < time.time():
            if token:
                SESSIONS.pop(token, None)
            return None
        return session

    def current_session_token(self) -> str | None:
        cookies = self.headers.get("Cookie", "")
        return next(
            (
                part.split("=", 1)[1]
                for part in cookies.split("; ")
                if part.startswith("vmportal_session=")
            ),
            None,
        )

    def current_role(self) -> str | None:
        session = self.current_session()
        return session.get("role", "user") if session else None

    def require_user(self) -> str | None:
        user = self.current_user()
        if not user:
            self.send_json({"error": "Authentication required"}, HTTPStatus.UNAUTHORIZED)
        return user

    def redirect(self, location: str) -> None:
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def proxy_spice_websocket(self, hostname: str) -> None:
        websocket_key = self.headers.get("Sec-WebSocket-Key", "")
        if self.headers.get("Upgrade", "").lower() != "websocket" or not websocket_key:
            return self.send_json({"error": "WebSocket upgrade required"}, HTTPStatus.UPGRADE_REQUIRED)
        target = local_qemu_spice_target(hostname)
        if not target:
            return self.send_json({"error": "SPICE console is not available"}, HTTPStatus.CONFLICT)
        try:
            spice_socket = socket.create_connection(target, timeout=10)
        except OSError as error:
            return self.send_json({"error": f"Unable to connect to SPICE console: {error}"}, HTTPStatus.BAD_GATEWAY)
        accept = base64.b64encode(
            hashlib.sha1((websocket_key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()
        ).decode()
        self.send_response(HTTPStatus.SWITCHING_PROTOCOLS)
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", accept)
        self.send_header("Sec-WebSocket-Protocol", "binary")
        self.end_headers()
        client_socket = self.connection
        client_socket.settimeout(None)
        spice_socket.settimeout(None)
        try:
            while True:
                readable, _, _ = select.select([client_socket, spice_socket], [], [], 30)
                if not readable:
                    websocket_send(client_socket, 0x9)
                    continue
                if spice_socket in readable:
                    data = spice_socket.recv(65536)
                    if not data:
                        break
                    websocket_send(client_socket, 0x2, data)
                if client_socket in readable:
                    opcode, payload = websocket_receive(client_socket)
                    if opcode == 0x8:
                        websocket_send(client_socket, 0x8)
                        break
                    if opcode == 0x9:
                        websocket_send(client_socket, 0xA, payload)
                    elif opcode in {0x0, 0x2}:
                        spice_socket.sendall(payload)
        except (ConnectionError, OSError, ValueError):
            pass
        finally:
            spice_socket.close()

    def do_GET(self) -> None:
        parsed_url = urlparse(self.path)
        path = parsed_url.path
        if path == "/api/console-websocket":
            token = parse_qs(parsed_url.query).get("token", [""])[0]
            with LOCK:
                ticket = CONSOLE_TICKETS.get(token)
                if ticket and ticket["expires"] < time.time():
                    CONSOLE_TICKETS.pop(token, None)
                    ticket = None
            if not ticket:
                return self.send_json({"error": "Invalid or expired console ticket"}, HTTPStatus.UNAUTHORIZED)
            return self.proxy_spice_websocket(ticket["hostname"])
        if path == "/api/health":
            return self.send_json({"status": "ok"})
        if path == "/api/auth/me":
            user = self.current_user()
            session = self.current_session()
            return self.send_json({
                "authenticated": bool(user),
                "username": user,
                "role": self.current_role(),
                "auth_source": session.get("auth_source") if session else None,
            })
        if path in {"/login.html", "/admin-login.html"} and self.current_user():
            return self.redirect("/")
        if path == "/" and not self.current_user():
            return self.redirect("/login.html")
        if path.startswith("/api/") and not self.require_user():
            return
        if path == "/api/config":
            return self.send_json({
                "profiles": PROFILES,
                "policy": POLICY,
                "environments": ENVIRONMENTS,
                "images": IMAGES,
                "networks": NETWORKS,
                "organization": CONFIG.get("organization", {}),
                "username": self.current_user(),
                "role": self.current_role(),
                "auth_source": self.current_session().get("auth_source", "ldap"),
                "provisioning_backend": PROVISIONING_BACKEND,
                "ssh_login_user": SSH_LOGIN_USER,
                "hostmaster": {
                    "email": HOSTMASTER_EMAIL,
                    "description": HOSTMASTER_DESCRIPTION,
                    "mode": "simulation",
                },
            })
        if path == "/api/settings/ssh-keys":
            return self.send_json(user_ssh_keys(self.current_user()))
        match = re.fullmatch(r"/api/requests/([a-z0-9-]+)/console-ticket", path)
        if match:
            request_id = match.group(1)
            with LOCK:
                requests = load_requests()
                request = next((item for item in requests if item["id"] == request_id), None)
                if not request:
                    return self.send_json({"error": "Request not found"}, HTTPStatus.NOT_FOUND)
                if self.current_role() != "admin" and request.get("requested_by") != self.current_user():
                    return self.send_json({"error": "You do not have access to this VM"}, HTTPStatus.FORBIDDEN)
                if request.get("status") != "completed" or local_qemu_domain_state(request["hostname"]) != "running":
                    return self.send_json({"error": "The VM is not running"}, HTTPStatus.CONFLICT)
                token = secrets.token_urlsafe(32)
                CONSOLE_TICKETS[token] = {
                    "hostname": request["hostname"],
                    "expires": time.time() + 600,
                }
            return self.send_json({
                "path": f"/api/console-websocket?token={token}",
                "expires_in": 600,
            })
        match = re.fullmatch(r"/api/requests/([a-z0-9-]+)/console-websocket", path)
        if match:
            request_id = match.group(1)
            with LOCK:
                requests = load_requests()
                request = next((item for item in requests if item["id"] == request_id), None)
                if not request:
                    return self.send_json({"error": "Request not found"}, HTTPStatus.NOT_FOUND)
                if self.current_role() != "admin" and request.get("requested_by") != self.current_user():
                    return self.send_json({"error": "You do not have access to this VM"}, HTTPStatus.FORBIDDEN)
                if request.get("status") != "completed" or local_qemu_domain_state(request["hostname"]) != "running":
                    return self.send_json({"error": "The VM is not running"}, HTTPStatus.CONFLICT)
                hostname = request["hostname"]
            return self.proxy_spice_websocket(hostname)
        match = re.fullmatch(r"/api/requests/([a-z0-9-]+)/console-screenshot", path)
        if match:
            request_id = match.group(1)
            with LOCK:
                requests = load_requests()
                request = next((item for item in requests if item["id"] == request_id), None)
                if not request:
                    return self.send_json({"error": "Request not found"}, HTTPStatus.NOT_FOUND)
                if self.current_role() != "admin" and request.get("requested_by") != self.current_user():
                    return self.send_json({"error": "You do not have access to this VM"}, HTTPStatus.FORBIDDEN)
                if request.get("status") != "completed" or not local_qemu_domain_exists(request["hostname"]):
                    return self.send_json({"error": "The completed VM no longer exists"}, HTTPStatus.CONFLICT)
                if local_qemu_domain_state(request["hostname"]) != "running":
                    return self.send_json({"error": "The VM is not running"}, HTTPStatus.CONFLICT)
                screenshot_path = Path(f"/tmp/vmportal-console-{request_id}.png")
                result = subprocess.run(
                    [
                        "virsh", "--connect", LOCAL_QEMU.get("uri", "qemu:///system"),
                        "screenshot", request["hostname"], str(screenshot_path), "--screen", "0",
                    ],
                    capture_output=True, text=True, timeout=15, check=False,
                )
                if result.returncode or not screenshot_path.is_file():
                    detail = (result.stderr or result.stdout).strip() or "Unable to capture VM console"
                    return self.send_json({"error": detail}, HTTPStatus.BAD_GATEWAY)
                image = screenshot_path.read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(image)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(image)
            return
        match = SNAPSHOT_DELETE_PLAN_PATH.fullmatch(path)
        if match:
            request_id, snapshot_id = match.groups()
            with LOCK:
                requests = load_requests()
                request = next((item for item in requests if item["id"] == request_id), None)
                if not request:
                    return self.send_json({"error": "Request not found"}, HTTPStatus.NOT_FOUND)
                if self.current_role() != "admin" and request.get("requested_by") != self.current_user():
                    return self.send_json({"error": "You do not have access to this VM"}, HTTPStatus.FORBIDDEN)
                plan, detail = manual_snapshot_delete_plan(request, snapshot_id)
            if not plan:
                return self.send_json({"error": detail}, HTTPStatus.NOT_FOUND)
            return self.send_json(plan)
        match = VM_DELETE_PLAN_PATH.fullmatch(path)
        if match:
            request_id = match.group(1)
            with LOCK:
                requests = load_requests()
                request = next((item for item in requests if item["id"] == request_id), None)
                if not request:
                    return self.send_json({"error": "Request not found"}, HTTPStatus.NOT_FOUND)
                if self.current_role() != "admin" and request.get("requested_by") != self.current_user():
                    return self.send_json({"error": "You do not have access to this VM"}, HTTPStatus.FORBIDDEN)
                plan, detail = vm_delete_plan(request)
            if not plan:
                return self.send_json({"error": detail}, HTTPStatus.CONFLICT)
            token = secrets.token_urlsafe(32)
            VM_DELETE_TICKETS[token] = {
                "request_id": request_id,
                "username": self.current_user(),
                "targets": plan["delete_targets"],
                "expires": time.time() + 300,
            }
            return self.send_json({
                "request_id": request_id,
                "hostname": plan["hostname"],
                "confirmation_token": token,
                "expires_in": 300,
                "failed_request": plan["failed_request"],
                "file_count": plan["file_count"],
                "file_names": [Path(file).name for file in plan["file_paths"]],
                "warning": ("The VM is absent. Its failed request and event history will be permanently removed."
                            if plan["failed_request"] else "This virtual machine will be permanently deleted."),
            })
        match = RESIZE_SNAPSHOT_CLEANUP_PLAN_PATH.fullmatch(path)
        if match:
            if self.current_role() != "admin":
                return self.send_json(
                    {"error": "Administrator access required"}, HTTPStatus.FORBIDDEN
                )
            request_id, snapshot_id = match.groups()
            with LOCK:
                requests = load_requests()
                request = next((item for item in requests if item["id"] == request_id), None)
                if not request:
                    return self.send_json({"error": "Request not found"}, HTTPStatus.NOT_FOUND)
                plan, detail = resize_snapshot_cleanup_plan(request, snapshot_id)
            if not plan:
                return self.send_json({"error": detail}, HTTPStatus.CONFLICT)
            return self.send_json(plan)
        if path == "/api/requests":
            with LOCK:
                requests = load_requests()
                if expire_resize_snapshots(requests):
                    save_requests(requests)
                reconcile_pending_ip(requests)
                if PROVISIONING_BACKEND == "local_qemu":
                    for item in requests:
                        if item.get("status") == "completed":
                            item["vm_state"] = local_qemu_domain_state(item["hostname"])
            if self.current_role() != "admin":
                requests = [item for item in requests if item.get("requested_by") == self.current_user()]
            return self.send_json(requests)
        if path.startswith("/api/"):
            return self.send_json({"error": "Not found"}, HTTPStatus.NOT_FOUND)
        if path == "/":
            self.path = "/index.html"
        return super().do_GET()

    def do_DELETE(self) -> None:
        path = urlparse(self.path).path
        authenticated_user = self.require_user()
        if not authenticated_user:
            return

        match = VM_DELETE_PATH.fullmatch(path)
        if match:
            try:
                payload = self.read_json()
            except json.JSONDecodeError:
                return self.send_json({"errors": ["Invalid JSON payload."]}, HTTPStatus.BAD_REQUEST)
            request_id = match.group(1)
            with LOCK:
                requests = load_requests()
                request = next((item for item in requests if item["id"] == request_id), None)
                if not request:
                    return self.send_json({"error": "Request not found"}, HTTPStatus.NOT_FOUND)
                if self.current_role() != "admin" and request.get("requested_by") != authenticated_user:
                    return self.send_json({"error": "You do not have access to this VM"}, HTTPStatus.FORBIDDEN)
                if str(payload.get("confirm_hostname", "")).strip() != request["hostname"]:
                    return self.send_json(
                        {"error": f"Type {request['hostname']} to confirm deletion"},
                        HTTPStatus.UNPROCESSABLE_ENTITY,
                    )
                confirmation_token = str(payload.get("confirmation_token", ""))
                ticket = VM_DELETE_TICKETS.pop(confirmation_token, None)
                if (
                    not ticket
                    or ticket.get("request_id") != request_id
                    or ticket.get("username") != authenticated_user
                    or ticket.get("expires", 0) < time.time()
                ):
                    return self.send_json(
                        {"error": "Deletion confirmation has expired; confirm the deletion again"},
                        HTTPStatus.UNPROCESSABLE_ENTITY,
                    )
                success, detail = delete_vm(request, ticket["targets"], payload.get("confirm_files") is True)
            if not success:
                return self.send_json(
                    {"error": "VM deletion failed", "detail": detail},
                    HTTPStatus.CONFLICT,
                )
            return self.send_json({"deleted": True, "id": request_id})

        match = re.fullmatch(r"/api/settings/ssh-keys/([A-Za-z0-9-]{1,64})", path)
        if not match:
            return self.send_json({"error": "Not found"}, HTTPStatus.NOT_FOUND)

        key_id = match.group(1)
        with database() as connection:
            deleted = connection.execute(
                "DELETE FROM ssh_keys WHERE id = ? AND username = ?",
                (key_id, authenticated_user),
            ).rowcount
        if not deleted:
            return self.send_json({"error": "SSH public key not found"}, HTTPStatus.NOT_FOUND)
        return self.send_json({"deleted": True, "id": key_id})

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        try:
            payload = self.read_json()
        except json.JSONDecodeError:
            return self.send_json({"errors": ["Invalid JSON payload."]}, HTTPStatus.BAD_REQUEST)

        if path == "/api/auth/login":
            username = str(payload.get("username", "")).strip()
            success, result = authenticate_ldap(username, str(payload.get("password", "")))
            log_authentication(username, self.client_address[0], result)
            if not success:
                message = "The LDAP service is currently unavailable." if result != "invalid_credentials" else "Invalid username or password."
                return self.send_json({"error": message}, HTTPStatus.UNAUTHORIZED)
            token = secrets.token_urlsafe(32)
            ttl = int(CONFIG.get("authentication", {}).get("session_ttl_hours", 8)) * 3600
            SESSIONS[token] = {
                "username": username,
                "role": user_role(username),
                "auth_source": "ldap",
                "expires": time.time() + ttl,
            }
            body = json.dumps({"authenticated": True, "username": username}).encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Set-Cookie", f"vmportal_session={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age={ttl}")
            self.end_headers()
            return self.wfile.write(body)

        if path == "/api/auth/admin-login":
            username = str(payload.get("username", "")).strip()
            if not authenticate_local_admin(username, str(payload.get("password", ""))):
                log_authentication(username, self.client_address[0], "invalid_credentials", "local_admin")
                return self.send_json(
                    {"error": "Invalid administrator username or password."},
                    HTTPStatus.UNAUTHORIZED,
                )
            log_authentication(username, self.client_address[0], "authenticated", "local_admin")
            token = secrets.token_urlsafe(32)
            ttl = int(CONFIG.get("authentication", {}).get("session_ttl_hours", 8)) * 3600
            SESSIONS[token] = {
                "username": username,
                "role": "admin",
                "auth_source": "local_admin",
                "expires": time.time() + ttl,
            }
            body = json.dumps({
                "authenticated": True,
                "username": username,
                "role": "admin",
            }).encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header(
                "Set-Cookie",
                f"vmportal_session={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age={ttl}",
            )
            self.end_headers()
            return self.wfile.write(body)

        if path == "/api/auth/logout":
            cookies = self.headers.get("Cookie", "")
            token = next((part.split("=", 1)[1] for part in cookies.split("; ") if part.startswith("vmportal_session=")), None)
            user = self.current_user()
            if token:
                SESSIONS.pop(token, None)
            if user:
                log_authentication(user, self.client_address[0], "logout")
            body = b'{"authenticated":false}'
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Set-Cookie", "vmportal_session=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0")
            self.end_headers()
            return self.wfile.write(body)

        authenticated_user = self.require_user()
        if not authenticated_user:
            return

        if path == "/api/admin/settings/password":
            session = self.current_session()
            if self.current_role() != "admin" or session.get("auth_source") != "local_admin":
                return self.send_json(
                    {"error": "Local administrator authentication required."},
                    HTTPStatus.FORBIDDEN,
                )
            current_password = str(payload.get("current_password", ""))
            new_password = str(payload.get("new_password", ""))
            confirmation = str(payload.get("confirm_password", ""))
            errors = []
            if not authenticate_local_admin(authenticated_user, current_password):
                errors.append("Current password is incorrect.")
            if len(new_password) < 12:
                errors.append("New password must be at least 12 characters long.")
            if len(new_password) > 256:
                errors.append("New password must be no more than 256 characters long.")
            if new_password != confirmation:
                errors.append("New password and confirmation do not match.")
            if current_password and secrets.compare_digest(current_password, new_password):
                errors.append("New password must be different from the current password.")
            if errors:
                return self.send_json({"errors": errors}, HTTPStatus.UNPROCESSABLE_ENTITY)

            with database() as connection:
                connection.execute(
                    "INSERT INTO local_admin_credentials(username, password_hash, updated_at) "
                    "VALUES (?, ?, ?) "
                    "ON CONFLICT(username) DO UPDATE SET "
                    "password_hash = excluded.password_hash, updated_at = excluded.updated_at",
                    (authenticated_user, hash_local_admin_password(new_password), now()),
                )
            current_token = self.current_session_token()
            for token, active_session in list(SESSIONS.items()):
                if (
                    token != current_token
                    and active_session.get("auth_source") == "local_admin"
                ):
                    SESSIONS.pop(token, None)
            log_authentication(
                authenticated_user,
                self.client_address[0],
                "password_changed",
                "local_admin",
            )
            return self.send_json({"changed": True})

        if path == "/api/settings/ssh-keys":
            label = str(payload.get("label", "")).strip()
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 ._@+-]{0,63}", label):
                return self.send_json(
                    {"errors": ["Key label must be between 1 and 64 characters."]},
                    HTTPStatus.UNPROCESSABLE_ENTITY,
                )
            login_user = str(payload.get("login_user", "")).strip().lower()
            if not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", login_user):
                return self.send_json(
                    {"errors": ["Linux login user must be a valid lowercase username."]},
                    HTTPStatus.UNPROCESSABLE_ENTITY,
                )
            public_key, fingerprint_or_error = validate_public_key(payload.get("public_key"))
            if not public_key:
                return self.send_json(
                    {"errors": [fingerprint_or_error]},
                    HTTPStatus.UNPROCESSABLE_ENTITY,
                )
            existing_keys = user_ssh_keys(authenticated_user)
            if any(
                key["fingerprint"] == fingerprint_or_error and key["login_user"] == login_user
                for key in existing_keys
            ):
                return self.send_json(
                    {"errors": ["This SSH public key is already saved."]},
                    HTTPStatus.CONFLICT,
                )
            key = {
                "id": str(uuid.uuid4())[:12],
                "label": label,
                "login_user": login_user,
                "public_key": public_key,
                "fingerprint": fingerprint_or_error,
                "created_at": now(),
            }
            with database() as connection:
                connection.execute(
                    "INSERT INTO ssh_keys(id, username, login_user, label, public_key, fingerprint, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        key["id"], authenticated_user, key["login_user"], key["label"], key["public_key"],
                        key["fingerprint"], key["created_at"],
                    ),
                )
            return self.send_json(key, HTTPStatus.CREATED)

        match = re.fullmatch(r"/api/requests/([a-z0-9-]+)/register-netbox", path)
        if match:
            with LOCK:
                request = next((item for item in load_requests() if item['id'] == match.group(1)), None)
                if not request:
                    return self.send_json({'error': 'Request not found'}, HTTPStatus.NOT_FOUND)
                if self.current_role() != 'admin' and request.get('requested_by') != authenticated_user:
                    return self.send_json({'error': 'You do not have access to this VM'}, HTTPStatus.FORBIDDEN)
                if request.get('status') != 'completed' or not NETBOX_ENABLED:
                    return self.send_json({'error': 'A completed VM and enabled NetBox integration are required'}, HTTPStatus.CONFLICT)
                success = register_netbox(request)
                save_requests([request])
            if not success:
                return self.send_json({'error': request['netbox']['error']}, HTTPStatus.BAD_GATEWAY)
            return self.send_json(request)

        if path == "/api/requests":
            clean, errors = validate(payload, authenticated_user)
            if errors:
                return self.send_json({"errors": errors}, HTTPStatus.UNPROCESSABLE_ENTITY)
            # The lock covers duplicate detection, preflight and reservation.
            # A second click waits until the first request has reserved the
            # hostname and then receives HTTP 409 instead of another record.
            with LOCK:
                requests = load_requests()
                duplicate = next((
                    item for item in requests
                    if request_blocks_hostname(item, clean["hostname"])
                ), None)
                if duplicate:
                    return self.send_json({"error": DUPLICATE_REQUEST_ERROR}, HTTPStatus.CONFLICT)
                storage_resources = dict(clean["resources"])
                storage_resources["disk_gb"] += sum(
                    disk["size_gb"] for disk in request_data_disks(clean)
                )
                placement, errors = preflight(clean["hostname"], storage_resources, clean["image"])
                if errors:
                    return self.send_json({"errors": errors}, HTTPStatus.UNPROCESSABLE_ENTITY)
                clean["placement"] = placement
                request = {
                    "id": str(uuid.uuid4())[:8],
                    **clean,
                    "requested_by": authenticated_user,
                    "created_at": now(),
                    "status": "awaiting_approval" if clean["approval_reasons"] else "provisioning",
                    "events": [
                        event("Request validated"),
                        event(
                            "Infrastructure preflight passed",
                            detail=(f"{placement['datastore']}: {placement['free_before_gb']} GB free, "
                                    f"{placement['free_after_gb']} GB after provisioning"),
                        ),
                    ],
                }
                if request["status"] == "awaiting_approval":
                    request["events"].append(event("Awaiting approval", "waiting", "; ".join(clean["approval_reasons"])))
                save_requests([request])
            if request["status"] == "provisioning":
                provision(request)
                with LOCK:
                    save_requests([request])
            return self.send_json(request, HTTPStatus.CREATED)

        match = re.fullmatch(
            (
                r"/api/requests/([a-z0-9-]+)/"
                r"(start|stop|reboot|retry-resize|rollback-resize|create-snapshot|"
                r"restore-snapshot|delete-snapshot|cleanup-resize-snapshot)"
            ),
            path,
        )
        if match:
            request_id, action = match.groups()
            if PROVISIONING_BACKEND != "local_qemu":
                return self.send_json(
                    {"error": "VM lifecycle operations require the local QEMU backend."},
                    HTTPStatus.NOT_IMPLEMENTED,
                )
            with LOCK:
                requests = load_requests()
                request = next((item for item in requests if item["id"] == request_id), None)
                if not request:
                    return self.send_json({"error": "Request not found"}, HTTPStatus.NOT_FOUND)
                if self.current_role() != "admin" and request.get("requested_by") != authenticated_user:
                    return self.send_json({"error": "You do not have access to this VM"}, HTTPStatus.FORBIDDEN)
                if request.get("status") != "completed" or not local_qemu_domain_exists(request["hostname"]):
                    return self.send_json({"error": "The completed VM no longer exists"}, HTTPStatus.CONFLICT)
                if action in {"start", "stop", "reboot"}:
                    success, detail = lifecycle_action(request, action, authenticated_user)
                elif action == "create-snapshot":
                    name = str(payload.get("name", "")).strip()
                    description = str(payload.get("description", "")).strip()
                    if not re.fullmatch(r"[\w .-]{1,64}", name, re.UNICODE):
                        return self.send_json(
                            {"error": "Snapshot name must be 1-64 letters, numbers, spaces, dots, underscores or hyphens"},
                            HTTPStatus.UNPROCESSABLE_ENTITY,
                        )
                    if len(description) > 300:
                        return self.send_json(
                            {"error": "Snapshot description may contain at most 300 characters"},
                            HTTPStatus.UNPROCESSABLE_ENTITY,
                        )
                    snapshot, detail = create_manual_snapshot(
                        request, name, description, authenticated_user
                    )
                    success = snapshot is not None
                elif action == "restore-snapshot":
                    snapshot_id = str(payload.get("snapshot_id", "")).strip()
                    if str(payload.get("confirm_hostname", "")).strip() != request["hostname"]:
                        return self.send_json(
                            {"error": f"Type {request['hostname']} to confirm snapshot restore"},
                            HTTPStatus.UNPROCESSABLE_ENTITY,
                        )
                    success, detail = restore_manual_snapshot(
                        request, snapshot_id, authenticated_user
                    )
                elif action == "delete-snapshot":
                    snapshot_id = str(payload.get("snapshot_id", "")).strip()
                    if str(payload.get("confirm_snapshot_id", "")).strip() != snapshot_id:
                        return self.send_json(
                            {"error": f"Type snapshot ID {snapshot_id} to confirm deletion"},
                            HTTPStatus.UNPROCESSABLE_ENTITY,
                        )
                    confirmed_targets = payload.get("confirmed_targets")
                    if not isinstance(confirmed_targets, list) or not all(
                        isinstance(target, str) for target in confirmed_targets
                    ):
                        return self.send_json(
                            {"error": "Confirmed deletion targets are required"},
                            HTTPStatus.UNPROCESSABLE_ENTITY,
                        )
                    success, detail = delete_manual_snapshot(
                        request, snapshot_id, authenticated_user, confirmed_targets
                    )
                elif action == "cleanup-resize-snapshot":
                    if self.current_role() != "admin":
                        return self.send_json(
                            {"error": "Administrator access required"}, HTTPStatus.FORBIDDEN
                        )
                    snapshot_id = str(payload.get("snapshot_id", "")).strip()
                    if str(payload.get("confirm_snapshot_id", "")).strip() != snapshot_id:
                        return self.send_json(
                            {"error": f"Type snapshot ID {snapshot_id} to confirm cleanup"},
                            HTTPStatus.UNPROCESSABLE_ENTITY,
                        )
                    confirmed_targets = payload.get("confirmed_targets")
                    if not isinstance(confirmed_targets, list) or not all(
                        isinstance(target, str) for target in confirmed_targets
                    ):
                        return self.send_json(
                            {"error": "Confirmed cleanup targets are required"},
                            HTTPStatus.UNPROCESSABLE_ENTITY,
                        )
                    success, detail = cleanup_expired_resize_snapshot(
                        request, snapshot_id, authenticated_user, confirmed_targets
                    )
                elif action == "retry-resize":
                    failed_resize = request.get("last_failed_resize")
                    if not isinstance(failed_resize, dict):
                        return self.send_json(
                            {"error": "No failed resize is available for retry"},
                            HTTPStatus.CONFLICT,
                        )
                    success, detail = resize_local_qemu(
                        request,
                        failed_resize["resources"],
                        failed_resize["additional_disks"],
                        authenticated_user,
                        create_snapshot=False,
                    )
                else:
                    if self.current_role() != "admin":
                        return self.send_json(
                            {"error": "Administrator role required for rollback"},
                            HTTPStatus.FORBIDDEN,
                        )
                    snapshot_id = str(payload.get("snapshot_id", "")).strip()
                    if str(payload.get("confirm_hostname", "")).strip() != request["hostname"]:
                        return self.send_json(
                            {"error": f"Type {request['hostname']} to confirm rollback"},
                            HTTPStatus.UNPROCESSABLE_ENTITY,
                        )
                    success, detail = rollback_resize_snapshot(
                        request,
                        snapshot_id,
                        authenticated_user,
                    )
                request["vm_state"] = local_qemu_domain_state(request["hostname"])
                save_requests(requests)
            if not success:
                return self.send_json(
                    {"error": f"VM {action} failed", "detail": detail},
                    HTTPStatus.CONFLICT,
                )
            return self.send_json(request)

        match = re.fullmatch(r"/api/requests/([a-z0-9-]+)/resize", path)
        if match:
            request_id = match.group(1)
            if PROVISIONING_BACKEND != "local_qemu":
                return self.send_json(
                    {"error": "Online resize is available only for the local QEMU backend in this PoC."},
                    HTTPStatus.NOT_IMPLEMENTED,
                )
            with LOCK:
                requests = load_requests()
                request = next((item for item in requests if item["id"] == request_id), None)
                if not request:
                    return self.send_json({"error": "Request not found"}, HTTPStatus.NOT_FOUND)
                if self.current_role() != "admin" and request.get("requested_by") != authenticated_user:
                    return self.send_json({"error": "You do not have access to this VM"}, HTTPStatus.FORBIDDEN)
                if request["status"] != "completed" or not local_qemu_domain_exists(request["hostname"]):
                    return self.send_json({"error": "The completed VM no longer exists"}, HTTPStatus.CONFLICT)
                resources, data_disks, approval_reasons, errors = validate_resize(payload, request)
                if errors:
                    return self.send_json({"errors": errors}, HTTPStatus.UNPROCESSABLE_ENTITY)
                if approval_reasons and self.current_role() != "admin":
                    request["status"] = "resize_awaiting_approval"
                    request["pending_resize"] = {
                        "resources": resources,
                        "additional_disks": data_disks,
                        "approval_reasons": approval_reasons,
                        "requested_by": authenticated_user,
                        "requested_at": now(),
                    }
                    request.pop("resize_rejected_by", None)
                    request["events"].append(event(
                        "Resize awaiting approval",
                        "waiting",
                        f"Requested by {authenticated_user}: {'; '.join(approval_reasons)}",
                    ))
                    save_requests(requests)
                    return self.send_json(request, HTTPStatus.ACCEPTED)
                errors = preflight_local_resize(request, resources, data_disks)
                if errors:
                    return self.send_json({"errors": errors}, HTTPStatus.UNPROCESSABLE_ENTITY)
                success, detail = resize_local_qemu(
                    request, resources, data_disks, authenticated_user
                )
                save_requests(requests)
            if not success:
                return self.send_json(
                    {"error": "Online resize failed. The VM was not rebooted.", "detail": detail},
                    HTTPStatus.CONFLICT,
                )
            return self.send_json(request)

        match = re.fullmatch(r"/api/requests/([a-z0-9-]+)/(approve|reject)", path)
        if match:
            request_id, action = match.groups()
            if self.current_role() != "admin":
                log_authentication(authenticated_user, self.client_address[0], "authorization_denied", f"{action}:{request_id}")
                return self.send_json({"error": "Administrator role required"}, HTTPStatus.FORBIDDEN)
            with LOCK:
                requests = load_requests()
                request = next((item for item in requests if item["id"] == request_id), None)
                if not request:
                    return self.send_json({"error": "Request not found"}, HTTPStatus.NOT_FOUND)
                if request["status"] not in {"awaiting_approval", "resize_awaiting_approval"}:
                    return self.send_json({"error": "Request is not awaiting approval"}, HTTPStatus.CONFLICT)
                if request["status"] == "resize_awaiting_approval":
                    pending_resize = request.get("pending_resize")
                    if not isinstance(pending_resize, dict):
                        return self.send_json(
                            {"error": "Pending resize data is missing"},
                            HTTPStatus.CONFLICT,
                        )
                    if action == "approve":
                        resources = pending_resize["resources"]
                        data_disks = pending_resize["additional_disks"]
                        errors = preflight_local_resize(
                            request,
                            resources,
                            data_disks,
                            retain_reserve=False,
                        )
                        if errors:
                            return self.send_json(
                                {"errors": errors},
                                HTTPStatus.UNPROCESSABLE_ENTITY,
                            )
                        request["events"].append(event(
                            "Resize request approved",
                            detail=authenticated_user,
                        ))
                        success, detail = resize_local_qemu(
                            request,
                            resources,
                            data_disks,
                            pending_resize.get("requested_by", request.get("requested_by", "unknown")),
                        )
                        request["status"] = "completed"
                        request.pop("pending_resize", None)
                        if success:
                            request["resize_approved_by"] = authenticated_user
                            request.pop("resize_rejected_by", None)
                        save_requests(requests)
                        if not success:
                            return self.send_json(
                                {
                                    "error": "Approved online resize failed. The VM was not rebooted.",
                                    "detail": detail,
                                },
                                HTTPStatus.CONFLICT,
                            )
                    else:
                        request["status"] = "completed"
                        request["resize_rejected_by"] = authenticated_user
                        rejection_reason = str(payload.get("reason", "")).strip()
                        request["events"].append(event(
                            "Resize request rejected",
                            "failed",
                            rejection_reason or authenticated_user,
                        ))
                        request.pop("pending_resize", None)
                        send_notification(
                            "failed",
                            request,
                            detail=(
                                f"The resource change was rejected by {authenticated_user}. "
                                f"Reason: {rejection_reason or 'no reason provided'}"
                            ),
                        )
                        save_requests(requests)
                    return self.send_json(request)
                if action == "approve":
                    request["approved_by"] = authenticated_user
                    request["events"].append(event("Request approved", detail=authenticated_user))
                    provision(request)
                else:
                    request["status"] = "rejected"
                    request["rejected_by"] = authenticated_user
                    rejection_reason = str(payload.get("reason", "")).strip()
                    request["rejection_reason"] = rejection_reason
                    request["events"].append(event(
                        "Request rejected",
                        "failed",
                        rejection_reason or authenticated_user,
                    ))
                    send_notification(
                        "failed",
                        request,
                        detail=(
                            f"The request was rejected by {authenticated_user}. "
                            f"Reason: {rejection_reason or 'no reason provided'}"
                        ),
                    )
                save_requests(requests)
            return self.send_json(request)

        return self.send_json({"error": "Not found"}, HTTPStatus.NOT_FOUND)


def resize_snapshot_expiration_worker() -> None:
    while True:
        time.sleep(60)
        try:
            with LOCK:
                requests = load_requests()
                if expire_resize_snapshots(requests):
                    save_requests(requests)
        except (OSError, sqlite3.Error, ValueError) as error:
            print(f"Resize snapshot expiration check failed: {error}")


if __name__ == "__main__":
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    database().close()
    threading.Thread(
        target=resize_snapshot_expiration_worker,
        name="resize-snapshot-expiration",
        daemon=True,
    ).start()
    print(f"VM portal listening on http://0.0.0.0:{PORT}")
    ThreadingHTTPServer(("0.0.0.0", PORT), PortalHandler).serve_forever()
