#!/bin/bash
# Create CT 200 — Postgres LXC for awareness
# Run on holodeck host: bash /tmp/create-ct200.sh
set -euo pipefail

echo "Creating CT 200 (awareness-pg)..."
pct create 200 local:vztmpl/debian-12-standard_12.12-1_amd64.tar.zst \
  --hostname awareness-pg \
  --cores 2 \
  --memory 2048 \
  --swap 512 \
  --rootfs local-lvm:20 \
  --net0 name=eth0,bridge=vmbr0,ip=192.168.200.100/24,gw=192.168.200.1 \
  --nameserver 192.168.200.1 \
  --unprivileged 1 \
  --features nesting=0 \
  --start 0 \
  --password

echo "CT 200 created. Starting..."
pct start 200
echo "CT 200 running."
