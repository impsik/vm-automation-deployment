#!/bin/sh
set -eu
chown -R portal:portal /data
exec python - "$@" <<'PY'
import os
import pwd
import sys

user = pwd.getpwnam('portal')
groups = {user.pw_gid}
socket = '/var/run/libvirt/libvirt-sock'
if os.path.exists(socket):
    groups.add(os.stat(socket).st_gid)
os.setgroups(sorted(groups))
os.setgid(user.pw_gid)
os.setuid(user.pw_uid)
# Dropping privileges does not change the environment inherited from root.
os.environ.update(HOME=user.pw_dir, USER=user.pw_name, LOGNAME=user.pw_name)
os.execvp(sys.argv[1], sys.argv[1:])
PY
