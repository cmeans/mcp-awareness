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

# Provision an awareness app LXC on holodeck.
# Usage: create-app-ct.sh <ct-id> <ip-suffix> <hostname>
# Example: create-app-ct.sh 210 110 awareness-app-a
#
# Run from holodeck host. Requires: pct, a Debian 12 template, and the
# workstation SSH public key at /tmp/awareness-ssh-key.pub on holodeck.
set -euo pipefail

CT_ID="${1:?Usage: create-app-ct.sh <ct-id> <ip-suffix> <hostname>}"
IP_SUFFIX="${2:?Usage: create-app-ct.sh <ct-id> <ip-suffix> <hostname>}"
HOSTNAME="${3:?Usage: create-app-ct.sh <ct-id> <ip-suffix> <hostname>}"
IP="192.168.200.${IP_SUFFIX}"

TEMPLATE=$(pveam list local | grep "debian-12-standard" | awk '{print $1}' | head -1)
if [[ -z "$TEMPLATE" ]]; then
    echo "Error: No Debian 12 template found. Run: pveam download local debian-12-standard_12.12-1_amd64.tar.zst" >&2
    exit 1
fi

SSH_KEY="/tmp/awareness-ssh-key.pub"
if [[ ! -f "$SSH_KEY" ]]; then
    echo "Error: SSH public key not found at $SSH_KEY" >&2
    echo "Copy your workstation key: scp ~/.ssh/id_ed25519.pub holodeck:/tmp/awareness-ssh-key.pub" >&2
    exit 1
fi

echo "Creating CT ${CT_ID} (${HOSTNAME}) at ${IP}..."

pct create "$CT_ID" "$TEMPLATE" \
    --hostname "$HOSTNAME" \
    --cores 1 \
    --memory 512 \
    --swap 256 \
    --rootfs local-lvm:8 \
    --net0 "name=eth0,bridge=vmbr0,ip=${IP}/24,gw=192.168.200.1" \
    --nameserver 192.168.200.10 \
    --unprivileged 1 \
    --features nesting=0 \
    --start 1 \
    --password

echo "Waiting for container to start..."
sleep 5

echo "Installing base packages..."
pct exec "$CT_ID" -- bash -c "apt update -qq && apt install -y -qq openssh-server sudo python3 python3-pip python3-venv python3-dev git build-essential libpq-dev curl > /dev/null 2>&1"

echo "Configuring SSH..."
pct exec "$CT_ID" -- bash -c "mkdir -p /root/.ssh && chmod 700 /root/.ssh"
pct push "$CT_ID" "$SSH_KEY" /root/.ssh/authorized_keys
pct exec "$CT_ID" -- bash -c "chmod 600 /root/.ssh/authorized_keys"

echo "Creating awareness user..."
pct exec "$CT_ID" -- bash -c "useradd --system --create-home --shell /bin/bash awareness"

echo "Cloning repo and installing..."
# NOTE: HTTPS clone requires the repo to be public, or a deploy key / credential
# helper configured on the container. If the repo is private, set up a read-only
# deploy key on each app node before running this script.
pct exec "$CT_ID" -- bash -c "mkdir -p /opt/mcp-awareness && chown awareness:awareness /opt/mcp-awareness"
pct exec "$CT_ID" -- bash -c "sudo -u awareness git clone https://github.com/cmeans/mcp-awareness.git /opt/mcp-awareness"
pct exec "$CT_ID" -- bash -c "sudo -u awareness python3 -m venv /opt/mcp-awareness/venv"
pct exec "$CT_ID" -- bash -c "sudo -u awareness /opt/mcp-awareness/venv/bin/pip install -e /opt/mcp-awareness"

echo "Creating CLI symlinks..."
pct exec "$CT_ID" -- bash -c "ln -sf /opt/mcp-awareness/venv/bin/mcp-awareness-token /usr/local/bin/"
pct exec "$CT_ID" -- bash -c "ln -sf /opt/mcp-awareness/venv/bin/mcp-awareness-user /usr/local/bin/"
pct exec "$CT_ID" -- bash -c "ln -sf /opt/mcp-awareness/venv/bin/mcp-awareness-secret /usr/local/bin/"
pct exec "$CT_ID" -- bash -c "ln -sf /opt/mcp-awareness/venv/bin/mcp-awareness-migrate /usr/local/bin/"

echo "Installing systemd service..."
pct exec "$CT_ID" -- bash -c 'cat > /etc/systemd/system/mcp-awareness.service << SVC
[Unit]
Description=MCP Awareness Server
After=network.target

[Service]
Type=simple
User=awareness
EnvironmentFile=/etc/awareness/env
ExecStart=/opt/mcp-awareness/venv/bin/mcp-awareness
Restart=on-failure
RestartSec=5
WorkingDirectory=/opt/mcp-awareness

[Install]
WantedBy=multi-user.target
SVC'
pct exec "$CT_ID" -- bash -c "systemctl daemon-reload && systemctl enable mcp-awareness"

echo "Creating env directory (env file must be copied separately)..."
pct exec "$CT_ID" -- bash -c "mkdir -p /etc/awareness && chmod 700 /etc/awareness"

echo ""
echo "CT ${CT_ID} (${HOSTNAME}) provisioned at ${IP}."
echo ""
echo "Next steps:"
echo "  1. Copy env file: pct exec ${CT_ID} -- bash -c 'cat > /etc/awareness/env << EOF'"
echo "     (paste contents from CT 201 or KeePass)"
echo "  2. Start service: pct exec ${CT_ID} -- systemctl start mcp-awareness"
echo "  3. Verify health: curl -s http://${IP}:8420/health | python3 -m json.tool"
