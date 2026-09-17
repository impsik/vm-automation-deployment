#!/usr/bin/env python3
"""Set exactly one selected SSH key for a named cloud-init user."""

from __future__ import annotations

import sys
from pathlib import Path

import yaml


def main() -> int:
    if len(sys.argv) != 4:
        print("usage: set-cloud-init-ssh-key.py USER_DATA LOGIN_USER PUBLIC_KEY", file=sys.stderr)
        return 2
    path = Path(sys.argv[1])
    login_user = sys.argv[2]
    public_key = sys.argv[3]
    document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    users = document.get("users")
    if not isinstance(users, list):
        raise ValueError("cloud-init template does not contain a users list")
    matching_user = next(
        (user for user in users if isinstance(user, dict) and user.get("name") == login_user),
        None,
    )
    if not matching_user:
        matching_user = {
            "name": login_user,
            "sudo": ["ALL=(ALL) NOPASSWD:ALL"],
            "groups": ["users", "sudo"],
            "shell": "/bin/bash",
            "lock_passwd": True,
        }
        users.append(matching_user)
    matching_user["ssh_authorized_keys"] = [public_key]
    path.write_text(
        "#cloud-config\n" + yaml.safe_dump(document, sort_keys=False, width=4096),
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
