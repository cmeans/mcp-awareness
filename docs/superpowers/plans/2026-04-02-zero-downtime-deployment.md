<!-- SPDX-License-Identifier: AGPL-3.0-or-later | Copyright (C) 2026 Chris Means -->
# Zero-Downtime Deployment Implementation Plan

> **For agentic workers:** This is an infrastructure provisioning plan, not a code implementation plan. Most steps are shell commands run on remote systems (holodeck, LXCs) that the user executes. The agent's role is to guide, verify, and troubleshoot. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the single-instance CT 201 with a HAProxy load balancer (CT 203) and two app instances (CT 210, 211), enabling zero-downtime rolling deploys with connection draining for MCP SSE sessions.

**Architecture:** CT 203 runs HAProxy on 192.168.200.103, load-balancing to CT 210 (192.168.200.110) and CT 211 (192.168.200.111). CT 202 (cloudflared) is reconfigured to point to the HAProxy instead of CT 201. A deploy script orchestrates hot deploys (code-only, zero-downtime) and maintenance deploys (with migrations, brief scheduled window).

**Tech Stack:** Proxmox VE 8.4, Debian 12, HAProxy 2.x, Python 3.11, socat, systemd

**Spec:** `docs/superpowers/specs/2026-04-02-zero-downtime-deployment-design.md`

---

## Conventions

- **`[holodeck]`** = SSH to holodeck host (192.168.200.70)
- **`[CT 200]`** = SSH/exec into CT 200 (awareness-pg, 192.168.200.100)
- **`[CT 201]`** = SSH/exec into CT 201 (awareness-app, 192.168.200.101) — being replaced
- **`[CT 202]`** = SSH/exec into CT 202 (awareness-tunnel, 192.168.200.102)
- **`[CT 203]`** = SSH/exec into CT 203 (awareness-lb, 192.168.200.103) — new
- **`[CT 210]`** = SSH/exec into CT 210 (awareness-app-a, 192.168.200.110) — new
- **`[CT 211]`** = SSH/exec into CT 211 (awareness-app-b, 192.168.200.111) — new
- **`[laptop]`** = Commands on Chris's Fedora workstation
- Steps marked **[USER]** require the user to provide secrets, make UI decisions, or run commands on systems the agent can't reach

---

## Task 1: Provision CT 203 — HAProxy LXC

**Where:** `[holodeck]`

- [ ] **Step 1: Identify the Debian 12 template**

```bash
pveam list local | grep debian-12
```

Expected: Shows a `debian-12-standard_*.tar.zst` template. Note the exact filename.

- [ ] **Step 2: Create the LXC**

```bash
pct create 203 local:vztmpl/debian-12-standard_12.12-1_amd64.tar.zst --hostname awareness-lb --cores 1 --memory 256 --swap 128 --rootfs local-lvm:4 --net0 name=eth0,bridge=vmbr0,ip=192.168.200.103/24,gw=192.168.200.1 --nameserver 192.168.200.1 --unprivileged 1 --features nesting=0 --start 0 --password
```

**[USER]** Set root password, store in KeePass.

Adjust the template filename if it differs from step 1.

- [ ] **Step 3: Start and enter CT 203**

```bash
pct start 203
pct enter 203
```

- [ ] **Step 4: Update base system**

```bash
apt update && apt upgrade -y
```

- [ ] **Step 5: Install HAProxy and socat**

```bash
apt install -y haproxy socat
haproxy -v
```

Expected: HAProxy version 2.6+ (Debian 12 ships 2.6.x).

- [ ] **Step 6: Install openssh-server and push SSH key**

```bash
apt install -y openssh-server
mkdir -p /root/.ssh && chmod 700 /root/.ssh
```

**[USER]** From holodeck host:
```bash
pct exec 203 -- tee /root/.ssh/authorized_keys <<< "YOUR_PUBLIC_KEY"
pct exec 203 -- chmod 600 /root/.ssh/authorized_keys
```

Verify from workstation:
```bash
ssh root@192.168.200.103 hostname
```

Expected: `awareness-lb`

- [ ] **Step 7: Configure HAProxy**

Create `/etc/haproxy/haproxy.cfg`:

```bash
cat > /etc/haproxy/haproxy.cfg << 'EOF'
global
    log /dev/log local0
    chroot /var/lib/haproxy
    stats socket /var/run/haproxy/admin.sock mode 660 level admin expose-fd listeners
    stats timeout 30s
    user haproxy
    group haproxy
    daemon
    maxconn 256

defaults
    log     global
    mode    http
    option  httplog
    option  dontlognull
    timeout connect 5s
    timeout client  300s
    timeout server  300s
    timeout http-keep-alive 300s

frontend awareness-front
    bind *:8420
    default_backend awareness-backend

backend awareness-backend
    balance roundrobin
    option httpchk GET /health
    http-check expect status 200
    stick-table type string len 64 size 10k expire 30m
    stick on req.hdr(mcp-session-id) if { req.hdr(mcp-session-id) -m found }
    server app-a 192.168.200.110:8420 check inter 5s fall 3 rise 2
    server app-b 192.168.200.111:8420 check inter 5s fall 3 rise 2

listen stats
    bind 192.168.200.103:8421
    stats enable
    stats uri /
    stats refresh 5s
    stats auth admin:haproxy-stats
EOF
```

**[USER]** Change the stats password (`admin:haproxy-stats`) to something from KeePass.

- [ ] **Step 8: Create runtime socket directory**

```bash
mkdir -p /var/run/haproxy
chown haproxy:haproxy /var/run/haproxy
```

- [ ] **Step 9: Validate config and restart**

```bash
haproxy -c -f /etc/haproxy/haproxy.cfg
systemctl enable haproxy
systemctl restart haproxy
systemctl status haproxy
```

Expected: Config valid, service active. Backends will show as DOWN until app LXCs are provisioned.

- [ ] **Step 10: Verify stats page**

From workstation:
```bash
curl -s -u admin:haproxy-stats http://192.168.200.103:8421/ | grep -c "app-a"
```

Expected: Non-zero (stats page is serving, shows backend names).

---

## Task 2: Create App LXC Provisioning Script

**Where:** `[laptop]`

This script automates creating new app LXCs with all operational fixes applied.

- [ ] **Step 1: Create the provisioning script**

Create `scripts/holodeck/create-app-ct.sh`:

```bash
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
    echo "Copy your workstation key: scp ~/.ssh/id_ed25519.pub root@192.168.200.70:/tmp/awareness-ssh-key.pub" >&2
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
    --nameserver 192.168.200.1 \
    --unprivileged 1 \
    --features nesting=0 \
    --start 1 \
    --password

echo "Waiting for container to start..."
sleep 5

echo "Installing base packages..."
pct exec "$CT_ID" -- bash -c "apt update -qq && apt install -y -qq openssh-server python3 python3-pip python3-venv python3-dev git build-essential libpq-dev > /dev/null 2>&1"

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
pct exec "$CT_ID" -- bash -c "cat > /etc/systemd/system/mcp-awareness.service << 'SVC'
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
SVC"
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
```

- [ ] **Step 2: Make executable**

```bash
chmod +x scripts/holodeck/create-app-ct.sh
```

- [ ] **Step 3: Commit**

```bash
git add scripts/holodeck/create-app-ct.sh
git commit -m "infra: add app LXC provisioning script for holodeck"
```

---

## Task 3: Provision CT 210 — App Instance A

**Where:** `[holodeck]`

- [ ] **Step 1: Copy SSH key to holodeck**

From workstation:
```bash
scp ~/.ssh/id_ed25519.pub root@192.168.200.70:/tmp/awareness-ssh-key.pub
```

- [ ] **Step 2: Copy provisioning script to holodeck**

From workstation:
```bash
scp scripts/holodeck/create-app-ct.sh root@192.168.200.70:/tmp/create-app-ct.sh
```

- [ ] **Step 3: Run provisioning script**

From holodeck:
```bash
bash /tmp/create-app-ct.sh 210 110 awareness-app-a
```

**[USER]** Set root password when prompted, store in KeePass.

Expected: Script completes with "CT 210 (awareness-app-a) provisioned at 192.168.200.110."

- [ ] **Step 4: Copy env file from CT 201**

From holodeck:
```bash
pct exec 201 -- cat /etc/awareness/env | pct exec 210 -- bash -c 'cat > /etc/awareness/env && chmod 600 /etc/awareness/env'
```

- [ ] **Step 5: Start the service**

```bash
pct exec 210 -- systemctl start mcp-awareness
pct exec 210 -- systemctl status mcp-awareness
```

Expected: Active (running).

- [ ] **Step 6: Verify health**

From holodeck:
```bash
curl -s http://192.168.200.110:8420/health | python3 -m json.tool
```

Expected: `{"status": "ok", ...}`

- [ ] **Step 7: Verify SSH from workstation**

From workstation:
```bash
ssh root@192.168.200.110 hostname
```

Expected: `awareness-app-a`

- [ ] **Step 8: Verify CLI tools**

```bash
ssh root@192.168.200.110 mcp-awareness-user list
```

Expected: Shows user list (may fail if env isn't sourced — the CLI tools need `AWARENESS_DATABASE_URL`). This is OK — the symlinks work, the env is for the systemd service.

---

## Task 4: Provision CT 211 — App Instance B

**Where:** `[holodeck]`

Repeat Task 3 with different parameters.

- [ ] **Step 1: Run provisioning script**

From holodeck:
```bash
bash /tmp/create-app-ct.sh 211 111 awareness-app-b
```

**[USER]** Set root password when prompted, store in KeePass.

- [ ] **Step 2: Copy env file from CT 201**

```bash
pct exec 201 -- cat /etc/awareness/env | pct exec 211 -- bash -c 'cat > /etc/awareness/env && chmod 600 /etc/awareness/env'
```

- [ ] **Step 3: Start and verify**

```bash
pct exec 211 -- systemctl start mcp-awareness
curl -s http://192.168.200.111:8420/health | python3 -m json.tool
```

Expected: `{"status": "ok", ...}`

- [ ] **Step 4: Verify SSH from workstation**

```bash
ssh root@192.168.200.111 hostname
```

Expected: `awareness-app-b`

---

## Task 5: Verify HAProxy Pool Health

**Where:** `[laptop]` and `[CT 203]`

Both app nodes should now be visible and healthy in HAProxy.

- [ ] **Step 1: Check HAProxy stats**

```bash
curl -s -u admin:haproxy-stats http://192.168.200.103:8421/\;csv | grep -E "app-a|app-b" | cut -d, -f1,2,18
```

Expected: Both `app-a` and `app-b` show status `UP`.

- [ ] **Step 2: Test traffic routing**

Send a request through HAProxy and verify it reaches an app node:

```bash
curl -s http://192.168.200.103:8420/health | python3 -m json.tool
```

Expected: `{"status": "ok", ...}` — response came from one of the app nodes via HAProxy.

- [ ] **Step 3: Test session stickiness**

Initialize an MCP session through HAProxy and verify subsequent requests go to the same backend:

```bash
TOKEN="<a valid JWT>"
SESSION=$(curl -sv http://192.168.200.103:8420/mcp -X POST -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" -H "Authorization: Bearer $TOKEN" -d '{"jsonrpc":"2.0","method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"test","version":"1.0"}},"id":1}' 2>&1 | grep -i 'mcp-session-id' | awk '{print $NF}' | tr -d '\r')
echo "Session: $SESSION"
```

Then make a follow-up call with the session ID:

```bash
curl -s http://192.168.200.103:8420/mcp -X POST -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" -H "Authorization: Bearer $TOKEN" -H "mcp-session-id: $SESSION" -d '{"jsonrpc":"2.0","method":"tools/call","params":{"name":"get_stats","arguments":{}},"id":2}' 2>&1 | grep -o '"total": [0-9]*'
```

Expected: Returns data (the session was routed to the same backend that created it).

- [ ] **Step 4: Test connection draining**

Set app-a to drain and verify new requests go to app-b:

```bash
ssh root@192.168.200.103 'echo "set server awareness-backend/app-a state drain" | socat stdio /var/run/haproxy/admin.sock'
```

Send a new request (no session ID — should go to app-b):

```bash
curl -s http://192.168.200.103:8420/health | python3 -m json.tool
```

Re-enable app-a:

```bash
ssh root@192.168.200.103 'echo "set server awareness-backend/app-a state ready" | socat stdio /var/run/haproxy/admin.sock'
```

---

## Task 6: Create Deploy Script

**Where:** `[laptop]`

- [ ] **Step 1: Create the deploy script**

Create `scripts/holodeck/deploy.sh`:

```bash
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

# Zero-downtime deploy for mcp-awareness on holodeck.
# Usage: deploy.sh hot              — rolling code update, zero-downtime
#        deploy.sh maintenance      — full stop, migrate, restart (scheduled)
set -euo pipefail

HAPROXY_HOST="192.168.200.103"
HAPROXY_SOCK="/var/run/haproxy/admin.sock"
APP_NODES=("192.168.200.110:app-a" "192.168.200.111:app-b")
DRAIN_TIMEOUT=60
HEALTH_TIMEOUT=30
HEALTH_INTERVAL=2

MODE="${1:?Usage: deploy.sh <hot|maintenance>}"

# --- Helpers ---

haproxy_cmd() {
    ssh "root@${HAPROXY_HOST}" "echo '$1' | socat stdio ${HAPROXY_SOCK}"
}

node_ip() { echo "${1%%:*}"; }
node_name() { echo "${1##*:}"; }

drain_node() {
    local name="$1"
    echo "  Draining ${name}..."
    haproxy_cmd "set server awareness-backend/${name} state drain"

    local waited=0
    while (( waited < DRAIN_TIMEOUT )); do
        local conns
        conns=$(haproxy_cmd "show stat" | grep "awareness-backend,${name}," | cut -d, -f5)
        if [[ "${conns:-0}" == "0" ]]; then
            echo "  ${name}: all connections drained"
            return 0
        fi
        echo "  ${name}: ${conns} active connections, waiting..."
        sleep 5
        waited=$((waited + 5))
    done
    echo "  WARNING: ${name} drain timeout (${DRAIN_TIMEOUT}s), proceeding anyway"
}

enable_node() {
    local name="$1"
    haproxy_cmd "set server awareness-backend/${name} state ready"
    echo "  ${name}: re-enabled"
}

update_node() {
    local ip="$1"
    echo "  Updating ${ip}..."
    ssh "root@${ip}" 'cd /opt/mcp-awareness && git pull origin main && venv/bin/pip install -e . -q && systemctl restart mcp-awareness'
}

wait_healthy() {
    local ip="$1"
    local waited=0
    while (( waited < HEALTH_TIMEOUT )); do
        if curl -sf "http://${ip}:8420/health" > /dev/null 2>&1; then
            echo "  ${ip}: healthy"
            return 0
        fi
        sleep "$HEALTH_INTERVAL"
        waited=$((waited + HEALTH_INTERVAL))
    done
    echo "  ERROR: ${ip} failed health check after ${HEALTH_TIMEOUT}s"
    return 1
}

# --- Hot deploy (rolling, zero-downtime) ---

hot_deploy() {
    echo "=== Hot deploy (zero-downtime) ==="
    for entry in "${APP_NODES[@]}"; do
        local ip name
        ip=$(node_ip "$entry")
        name=$(node_name "$entry")

        echo ""
        echo "--- ${name} (${ip}) ---"
        drain_node "$name"
        update_node "$ip"

        if wait_healthy "$ip"; then
            enable_node "$name"
        else
            echo "  ALERT: ${name} failed health check — leaving drained!"
            echo "  Manual intervention required."
            # Continue to next node — don't leave the whole service down
        fi
    done

    echo ""
    echo "=== Hot deploy complete ==="
}

# --- Maintenance deploy (full stop, migrate, restart) ---

maintenance_deploy() {
    echo "=== Maintenance deploy ==="
    echo ""

    # Drain all nodes
    echo "Step 1: Draining all nodes..."
    for entry in "${APP_NODES[@]}"; do
        drain_node "$(node_name "$entry")"
    done

    echo ""
    echo "Step 2: All nodes drained. Running migration..."
    local first_ip
    first_ip=$(node_ip "${APP_NODES[0]}")
    ssh "root@${first_ip}" 'cd /opt/mcp-awareness && git pull origin main && venv/bin/pip install -e . -q'
    ssh "root@${first_ip}" 'cd /opt/mcp-awareness && set -a && source /etc/awareness/env && set +a && venv/bin/mcp-awareness-migrate upgrade head'
    echo "  Migration complete on ${first_ip}"

    echo ""
    echo "Step 3: Updating and restarting all nodes..."
    for entry in "${APP_NODES[@]}"; do
        local ip
        ip=$(node_ip "$entry")
        update_node "$ip"
        wait_healthy "$ip" || echo "  WARNING: ${ip} not healthy yet"
    done

    echo ""
    echo "Step 4: Re-enabling all nodes..."
    for entry in "${APP_NODES[@]}"; do
        enable_node "$(node_name "$entry")"
    done

    echo ""
    echo "=== Maintenance deploy complete ==="
}

# --- Main ---

case "$MODE" in
    hot)
        hot_deploy
        ;;
    maintenance)
        echo "This will briefly take the service offline for migrations."
        read -p "Continue? [y/N] " -r
        [[ $REPLY =~ ^[Yy]$ ]] || exit 0
        maintenance_deploy
        ;;
    *)
        echo "Usage: deploy.sh <hot|maintenance>" >&2
        exit 1
        ;;
esac
```

- [ ] **Step 2: Make executable**

```bash
chmod +x scripts/holodeck/deploy.sh
```

- [ ] **Step 3: Commit**

```bash
git add scripts/holodeck/deploy.sh
git commit -m "infra: add zero-downtime deploy script (hot + maintenance modes)"
```

---

## Task 7: Switch Tunnel to HAProxy

**Where:** `[CT 202]`

This is the cutover — traffic starts flowing through HAProxy.

- [ ] **Step 1: Verify CT 201 is still serving (fallback ready)**

```bash
curl -s http://192.168.200.101:8420/health | python3 -m json.tool
```

Expected: `{"status": "ok", ...}`

- [ ] **Step 2: Update tunnel config**

SSH to CT 202 and update the cloudflared config:

```bash
ssh root@192.168.200.102
```

Edit `/etc/cloudflared/config.yml` — change the service URL from `http://192.168.200.101:8420` to `http://192.168.200.103:8420`:

```bash
nano /etc/cloudflared/config.yml
```

The `ingress` section should now point to the HAProxy:
```yaml
ingress:
  - hostname: staging.mcpawareness.com
    service: http://192.168.200.103:8420
  - service: http_status:404
```

- [ ] **Step 3: Restart cloudflared**

```bash
systemctl restart cloudflared
systemctl status cloudflared
journalctl -u cloudflared -n 10 --no-pager
```

Expected: Active, "Connection established", "Registered tunnel connection".

- [ ] **Step 4: Verify end-to-end**

From workstation, test via the public URL:

```bash
curl -s https://staging.mcpawareness.com/health 2>&1 | head -5
```

Expected: Health response (or auth challenge, depending on mount path config).

**[USER]** Test from Claude Desktop:
1. Disconnect and reconnect the Awareness connector
2. Call `get_briefing` — should return your data
3. Call `get_stats` — should show your 543+ entries

---

## Task 8: Update Proxmox Resource Pool and Snapshots

**Where:** `[holodeck]`

- [ ] **Step 1: Create resource pool**

```bash
pvesh create /pools --poolid awareness
pvesh set /pools/awareness --vms 200,202,203,210,211
```

- [ ] **Step 2: Set boot order for new containers**

```bash
pct set 203 --onboot 1 --startup order=2,up=5
pct set 210 --onboot 1 --startup order=3,up=15
pct set 211 --onboot 1 --startup order=3,up=15
```

Boot order: CT 200 (Postgres, order=1) → CT 203 (HAProxy, order=2, 5s delay) → CT 210+211 (apps, order=3, 15s delay for Postgres) → CT 202 (tunnel, order=4).

- [ ] **Step 2b: Verify CT 202 boot order**

CT 202 (tunnel) must boot after HAProxy (CT 203) and the app nodes, otherwise cloudflared starts before its upstream is available.

```bash
pct config 202 | grep -E 'onboot|startup'
```

If order is not set or is lower than 4, fix it:

```bash
pct set 202 --onboot 1 --startup order=4,up=5
```

- [ ] **Step 3: Update snapshot script**

Edit `/usr/local/bin/awareness-snapshots.sh` on holodeck:

Change:
```bash
for ct in 200 201 202; do
```

To:
```bash
for ct in 200 202 203 210 211; do
```

- [ ] **Step 4: Verify snapshot script**

```bash
bash /usr/local/bin/awareness-snapshots.sh
```

Expected: Creates snapshots for all 5 containers.

---

## Task 9: Update SSH Config on Workstation

**Where:** `[laptop]`

- [ ] **Step 1: Add SSH aliases**

Add to `~/.ssh/config`:

```
Host awareness-lb
    HostName 192.168.200.103
    User root

Host awareness-app-a
    HostName 192.168.200.110
    User root

Host awareness-app-b
    HostName 192.168.200.111
    User root
```

- [ ] **Step 2: Verify**

```bash
ssh awareness-lb hostname
ssh awareness-app-a hostname
ssh awareness-app-b hostname
```

Expected: `awareness-lb`, `awareness-app-a`, `awareness-app-b`

---

## Task 10: Test Hot Deploy

**Where:** `[laptop]`

- [ ] **Step 1: Run a hot deploy**

```bash
scripts/holodeck/deploy.sh hot
```

Expected output:
```
=== Hot deploy (zero-downtime) ===

--- app-a (192.168.200.110) ---
  Draining app-a...
  app-a: all connections drained
  Updating 192.168.200.110...
  192.168.200.110: healthy
  app-a: re-enabled

--- app-b (192.168.200.111) ---
  Draining app-b...
  app-b: all connections drained
  Updating 192.168.200.111...
  192.168.200.111: healthy
  app-b: re-enabled

=== Hot deploy complete ===
```

- [ ] **Step 2: Verify service is healthy after deploy**

```bash
curl -s http://192.168.200.103:8420/health | python3 -m json.tool
```

Expected: `{"status": "ok", ...}`

- [ ] **Step 3: Verify from Claude Desktop**

**[USER]** Call `get_briefing` from Claude Desktop. Should work without reconnecting — existing sessions should have survived (they were drained, not killed).

---

## Task 11: Decommission CT 201

**Where:** `[holodeck]`

Do this one week after successful cutover, not immediately.

- [ ] **Step 1: Stop the service**

```bash
pct exec 201 -- systemctl stop mcp-awareness
pct exec 201 -- systemctl disable mcp-awareness
```

- [ ] **Step 2: Keep the container for one week**

Leave CT 201 running but with the awareness service stopped. If anything goes wrong, you can:
```bash
pct exec 201 -- systemctl start mcp-awareness
```
And revert CT 202's tunnel config back to `192.168.200.101:8420`.

- [ ] **Step 3: After one week — destroy**

```bash
pct stop 201
pct destroy 201
```

Remove CT 201 from any snapshot scripts or resource pools if it was added.

---

## Task 12: Update Documentation

**Where:** `[laptop]`

- [ ] **Step 1: Update maintenance guide**

Update `docs/maintenance/holodeck/update-mcp-awareness.md` to reference the deploy script instead of manual steps:

Replace the manual steps with:

```markdown
## Deploying Updates

### Code-only updates (zero-downtime)

```bash
scripts/holodeck/deploy.sh hot
```

### Updates with migrations or config changes

```bash
scripts/holodeck/deploy.sh maintenance
```

See `docs/superpowers/specs/2026-04-02-zero-downtime-deployment-design.md` for details.
```

- [ ] **Step 2: Update deployment design spec topology diagram**

Update `docs/superpowers/specs/2026-04-01-holodeck-deployment-design.md` topology section to reflect the new architecture (HAProxy + app pool instead of single CT 201).

- [ ] **Step 3: Commit**

```bash
git add docs/
git commit -m "docs: update maintenance guide and topology for zero-downtime deploy"
```
