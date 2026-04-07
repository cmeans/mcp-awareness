<!-- SPDX-License-Identifier: AGPL-3.0-or-later | Copyright (C) 2026 Chris Means -->
# Update MCP Awareness on Holodeck

The mcp-awareness service runs on two app nodes (CT 210, CT 211) behind an HAProxy load balancer (CT 203). Updates are deployed using the zero-downtime deploy script.

## Prerequisites

- SSH access to holodeck and all CTs (via `~/.ssh/config` aliases)
- The deploy script at `scripts/holodeck/deploy.sh`

## Deploying Updates

### Code-only updates (zero-downtime)

```bash
scripts/holodeck/deploy.sh hot
```

This performs a rolling update: drains each node from HAProxy, pulls latest code, installs, restarts the service, waits for health check, then re-enables. One node is always serving traffic.

**Note:** Active MCP sessions on the restarting node will get "Session terminated" errors. Clients need to reconnect. See issues #161–#163 for planned improvements.

### Updates with migrations or config changes

```bash
scripts/holodeck/deploy.sh maintenance
```

This drains all nodes, runs Alembic migrations on the first node, then updates and restarts all nodes. There is a brief service interruption during migration.

### Adding new environment variables

If a release requires new env vars, update the env file on both app nodes before deploying:

```bash
ssh awareness-app-a 'nano /etc/awareness/env'
ssh awareness-app-b 'nano /etc/awareness/env'
```

## Verification

After deploy, verify via HAProxy:

```bash
curl -s http://192.168.200.103:8420/health | python3 -m json.tool
```

Or check both backends directly:

```bash
curl -s http://192.168.200.110:8420/health | python3 -m json.tool
curl -s http://192.168.200.111:8420/health | python3 -m json.tool
```

## Architecture

See `docs/superpowers/specs/2026-04-02-zero-downtime-deployment-design.md` for the full design spec.

| Component | Host | IP |
|-----------|------|----|
| HAProxy (load balancer) | CT 203 `awareness-lb` | 192.168.200.103 |
| App node A | CT 210 `awareness-app-a` | 192.168.200.110 |
| App node B | CT 211 `awareness-app-b` | 192.168.200.111 |
| Postgres | CT 200 `awareness-pg` | 192.168.200.100 |
| Cloudflare tunnel | CT 202 `awareness-tunnel` | 192.168.200.102 |
