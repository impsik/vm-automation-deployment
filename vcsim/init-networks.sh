#!/bin/sh
set -eu

attempt=0
until govc about >/dev/null 2>&1; do
  attempt=$((attempt + 1))
  if [ "$attempt" -ge 30 ]; then
    echo "vcsim did not become ready" >&2
    exit 1
  fi
  sleep 1
done

while true; do
  for portgroup in \
    "172.17.100.0-24 Admin network" \
    poc-net-app \
    poc-net-web \
    poc-net-integration \
    poc-net-development \
    poc-net-test \
    poc-net-backup \
    poc-net-monitoring \
    poc-net-management \
    poc-net-database
  do
    if ! govc find /DC0/network -type n -name "$portgroup" | grep -q .; then
      govc dvs.portgroup.add -dvs DVS0 "$portgroup"
    fi
  done
  touch /tmp/networks-ready
  sleep 30
done
