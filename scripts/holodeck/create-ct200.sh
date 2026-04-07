#!/usr/bin/env bash
# mcp-awareness — ambient system awareness for AI agents
# Copyright (C) 2026 Chris Means
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

# Create CT 200 — Postgres LXC for awareness.
# No arguments — creates CT 200 with fixed parameters (IP .100, 2 cores, 2GB RAM).
# Run on holodeck host: bash create-ct200.sh
set -euo pipefail

echo "Creating CT 200 (awareness-pg)..."
pct create 200 local:vztmpl/debian-12-standard_12.12-1_amd64.tar.zst \
  --hostname awareness-pg \
  --cores 2 \
  --memory 2048 \
  --swap 512 \
  --rootfs local-lvm:20 \
  --net0 name=eth0,bridge=vmbr0,ip=192.168.200.100/24,gw=192.168.200.1 \
  --nameserver 192.168.200.10 \
  --unprivileged 1 \
  --features nesting=0 \
  --start 0 \
  --password

echo "CT 200 created. Starting..."
pct start 200
echo "CT 200 running."
