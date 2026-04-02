<!-- SPDX-License-Identifier: AGPL-3.0-or-later | Copyright (C) 2026 Chris Means -->
# Update MCP Awareness on Holodeck

Manual deployment steps for updating the mcp-awareness service on the holodeck Proxmox host (CT 201 — `awareness-app`).

## Prerequisites

- SSH access to holodeck (`192.168.200.70`)
- Root access on CT 201 (`awareness-app`, `192.168.200.101`)

## Steps

### 1. SSH into the container

```bash
ssh root@192.168.200.101
```

### 2. Pull latest code

```bash
git config --global --add safe.directory /opt/mcp-awareness
cd /opt/mcp-awareness
git pull origin main
```

### 3. Install updated package

```bash
/opt/mcp-awareness/venv/bin/pip install -e .
```

### 4. Add any new environment variables

If the release includes new env vars, append them to the env file:

```bash
vi /etc/awareness/env
```

### 5. Restart the service

```bash
systemctl restart mcp-awareness
```

### 6. Verify

```bash
curl -s localhost:8420/health | python3 -m json.tool
```

Confirm `status: ok` and expected uptime (should be a few seconds).
