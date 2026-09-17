#!/bin/sh
set -eu
chown -R portal:portal /data
exec su-exec portal "$@"
