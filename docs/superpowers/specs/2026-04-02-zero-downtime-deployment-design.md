<!-- SPDX-License-Identifier: AGPL-3.0-or-later | Copyright (C) 2026 Chris Means -->
# Zero-Downtime Deployment Design

**Goal:** Enable zero-downtime deploys for mcp-awareness on holodeck so external users are never disrupted by service updates. Solve operational pain points from the initial deployment. Prepare for future cloud/k8s migration.

**Problem:** CT 201 (awareness-app) is a single instance. `systemctl restart mcp-awareness` drops all active MCP connections. With external users connecting via OAuth, this causes visible disruption. The current manual deploy process (SSH, git pull, pip install, restart) is also error-prone and lacks operational niceties (SSH access, CLI tools on PATH).

---

## 1. Topology

### New containers

| CT ID | Hostname | IP | Role | Resources |
|-------|----------|----|------|-----------|
| 203 | awareness-lb | 192.168.200.103 | HAProxy load balancer | 1 core, 256MB, 4GB disk |
| 210 | awareness-app-a | 192.168.200.110 | App instance A | 1 core, 512MB, 8GB disk |
| 211 | awareness-app-b | 192.168.200.111 | App instance B | 1 core, 512MB, 8GB disk |

### Retired

CT 201 (`awareness-app`) is decommissioned after migration. Its IP (192.168.200.101) is not reused.

### Updated tunnel config

CT 202 (cloudflared) changes upstream from `192.168.200.101:8420` to `192.168.200.103:8420`.

### Traffic flow

```
Internet -> Cloudflare -> CT 202 (tunnel) -> CT 203 (HAProxy :8420)
                                                |-> CT 210 (app-a :8420)
                                                |-> CT 211 (app-b :8420)
```

### Proxmox resource pool

All awareness CTs grouped under an `awareness` pool:
```bash
pvesh create /pools --poolid awareness
pvesh set /pools/awareness --vms 200,202,203,210,211
```

### Future scaling

Additional app nodes (CT 212, 213, ...) can be added to the pool. HAProxy round-robins across all healthy backends. The A/B deploy pattern generalizes to rolling updates across N nodes.

---

## 2. HAProxy Configuration

### Mode and routing

- **Mode:** `http` (layer 7) -- enables HTTP health checks, header inspection, and session stickiness
- **Frontend:** binds `:8420`, forwards to backend pool
- **Backend:** round-robin across app nodes, HTTP mode

### Health checks

- HTTP GET to `/health` every 5 seconds
- 3 consecutive failures removes node from pool
- Node re-added automatically when health checks pass again

### Connection draining (key feature for MCP)

MCP uses long-lived SSE connections. When a node is set to `drain` state via HAProxy's runtime API:
- No new connections are routed to the node
- Existing SSE/MCP sessions continue until they close naturally
- The deploy script waits for active connection count to reach 0 (or timeout after 60s)

This is HAProxy's core advantage over nginx for this workload.

### Session stickiness

MCP streamable-http sessions are stateful (in-process session state). A session started on app-a must stay on app-a.

HAProxy sticky sessions via `stick-table` keyed on the `mcp-session-id` request header:
- The `initialize` call (no session ID yet) gets round-robined
- The response includes `mcp-session-id` header
- Subsequent requests with that header are routed to the same backend

### HAProxy runtime API

Unix socket at `/var/run/haproxy/admin.sock` for runtime commands:
- `set server backend/app-a state drain` -- stop new connections
- `set server backend/app-a state ready` -- resume
- `show stat` -- connection counts, health status
- No config reload needed for drain/ready transitions

### Stats page

Built-in stats page on `:8421`, bound to LAN only (`bind 192.168.200.103:8421`). Shows real-time backend health, connection counts, drain status. Sufficient for deploy monitoring; Prometheus/Grafana is a future sub-project.

### Unauthenticated paths

HAProxy passes all traffic through without inspecting auth. Authentication is handled entirely by the app's own middleware. HAProxy is a load balancer, not a security boundary.

---

## 3. Deploy Modes

A single deploy script orchestrates both modes. Located in `mcp-awareness-infra` repo (or `scripts/holodeck/deploy.sh` until infra repo is set up).

### Hot deploy (code-only, zero-downtime)

Usage: `deploy.sh hot`

For each node in pool (one at a time):
1. Set node to `drain` via HAProxy runtime socket
2. Wait for active connections to reach 0 (timeout 60s)
3. SSH to node: `cd /opt/mcp-awareness && git pull origin main && venv/bin/pip install -e . && systemctl restart mcp-awareness`
4. Wait for `/health` to return OK (poll every 2s, timeout 30s)
5. Set node back to `ready`

If any node fails health check after update, leave it drained and alert (notify-send + awareness `report_alert`).

### Maintenance deploy (migrations/config, brief downtime)

Usage: `deploy.sh maintenance`

1. Post maintenance notice via awareness `report_alert` (users see it in their next briefing)
2. Drain all nodes, wait for connections to close (timeout 60s)
3. SSH to one node: run Alembic migration against shared Postgres
4. If env var changes needed: update `/etc/awareness/env` on all nodes
5. Update all nodes: git pull, pip install, restart
6. Health check all nodes
7. Re-enable all nodes in HAProxy
8. Clear maintenance alert

### HAProxy interaction

Deploy script uses `socat` to send commands to the HAProxy runtime socket:
```bash
echo "set server awareness-backend/app-a state drain" | socat stdio /var/run/haproxy/admin.sock
```

---

## 4. App LXC Provisioning

New app LXCs are provisioned identically via a script. Fixes all CT 201 operational pain points.

### Provisioning script

`scripts/holodeck/create-app-ct.sh <ct-id> <ip>` — creates an app LXC from scratch:

1. Create LXC from Debian 12 template (1 core, 512MB, 8GB)
2. Install openssh-server, push workstation SSH key
3. Install Python 3, git, build-essential, libpq-dev
4. Create `awareness` system user
5. Clone repo, create venv, pip install
6. Symlink CLI tools to `/usr/local/bin/` (mcp-awareness-token, -user, -secret, -migrate)
7. Copy `/etc/awareness/env` from an existing node (or template)
8. Install systemd service
9. Start and verify health

### SSH config

Each new node gets an alias in `~/.ssh/config`:
```
Host awareness-app-a
    HostName 192.168.200.110
    User root

Host awareness-app-b
    HostName 192.168.200.111
    User root

Host awareness-lb
    HostName 192.168.200.103
    User root
```

### Adding more nodes

To add a third app node:
1. Run `create-app-ct.sh 212 192.168.200.112`
2. Add the new backend to HAProxy config and reload
3. Add CT to Proxmox resource pool
4. Update deploy script's node list

---

## 5. Migration from CT 201

### Steps

1. Provision CT 203 (HAProxy), CT 210 (app-a), CT 211 (app-b)
2. Configure HAProxy with both app backends
3. Copy `/etc/awareness/env` from CT 201 to CT 210 and CT 211
4. Verify both app nodes serve `/health` and pass tenant isolation
5. Update CT 202 tunnel config: change upstream to `192.168.200.103:8420`
6. Restart cloudflared on CT 202
7. Verify end-to-end: Claude Desktop connects, tools work, data intact
8. Stop CT 201 service, keep the container for one week as fallback
9. After one week with no issues, decommission CT 201

### Rollback

If anything goes wrong after step 5, revert CT 202's tunnel config back to `192.168.200.101:8420` and restart cloudflared. CT 201 is still running and serving.

---

## 6. Backup Updates

### Postgres backup (CT 200)

Unchanged. The daily pg_dump backs up the shared database regardless of how many app nodes exist.

### Proxmox snapshots

Update the weekly snapshot script on holodeck to include the new containers:
- Current: CTs 200, 201, 202
- Updated: CTs 200, 202, 203, 210, 211 (remove 201 after decommission)

### App node config

`/etc/awareness/env` contains secrets (JWT secret, OAuth config, DB password). These files must be recoverable if a node is destroyed.

Options (pick one during implementation):
- **KeePass only** (current approach) -- env values are already stored there, reprovision from KeePass if needed
- **Backup to NAS** -- cron on CT 200 pulls env files from app nodes via SSH to the NFS backup share
- **Template in infra repo** -- env file template (with placeholder values) in the infra repo, secrets filled from KeePass

KeePass is sufficient for now. The provisioning script can pull from a template.

---

## 7. Not In Scope

- Prometheus/Grafana monitoring (future sub-project, HAProxy has exporter ready)
- CI/CD pipeline (deploys are scripted but manually triggered for now)
- Local k8s / k3s (future sub-project when container count or deploy frequency justifies it)
- Auto-scaling (manual node addition)
- Purpose-built user notification system (use awareness `report_alert` for maintenance windows)
- Automated rollback on failed deploy (manual rollback via deploy script or tunnel revert)
