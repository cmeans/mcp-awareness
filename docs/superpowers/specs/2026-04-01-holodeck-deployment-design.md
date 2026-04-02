<!-- SPDX-License-Identifier: AGPL-3.0-or-later | Copyright (C) 2026 Chris Means -->
# Holodeck Deployment Design

**Date:** 2026-04-01
**Status:** Draft — awaiting user review
**Goal:** Move MCP Awareness from Chris's laptop to the Proxmox host "holodeck" so the service runs independently, with OAuth enabled out of the box and load profiling for future cloud migration.

## Context

Awareness currently runs as a Docker Compose stack on Chris's Fedora laptop. This means:
- Service goes down when the laptop sleeps, travels, or reboots
- No load profiling data for cloud cost estimation
- OAuth staging stack (`staging.mcpawareness.com`) also runs on the laptop

Holodeck is a Proxmox VE host with 40 Xeon threads, 128GB RAM, 2x Quadro P4000 GPUs, and 737GB free on local-lvm. Ollama is already running bare metal with CUDA. This is a much better home for awareness.

## Decisions made

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Topology | 3 LXCs + bare metal Ollama | Per-component isolation, independent lifecycle, clean resource metrics for cloud sizing |
| No Docker for awareness | pip install from git, systemd | Direct resource metrics from Proxmox, no Docker-in-LXC abstraction layer |
| OAuth from day one | `docker-compose.oauth.yaml` config values | WorkOS AuthKit already tested and working on staging |
| Default owner | `admin` (system-level, non-loginable) | Owns pre-OAuth data and CLI operations; Chris's OAuth identity is the real admin |
| Data migration | pg_dump from laptop → pg_restore on holodeck | Laptop stack stays as fallback |
| Multi-tenant migration first | Run on laptop before dump | Schema must be current for data to load on holodeck |
| Ollama connectivity | Bare metal on host, `192.168.200.70:11434` | Already running with GPU/CUDA, no passthrough needed |
| Backups | Daily pg_dump to Synology NAS via NFS | Off-box protection, encrypted share, 30-day retention |
| Cloud path | LXCs now → Cloud Run v1 → k8s v2 | LXC metrics map directly to Cloud Run sizing; k8s when service count/users justify it |

## Topology

```
┌──────────────────────────────────────────────────────────────┐
│  holodeck (192.168.200.70)  ·  Proxmox VE 8.4.1             │
│                                                              │
│  ┌──────────────────┐  ┌──────────────────┐  ┌────────────┐ │
│  │ CT 200           │  │ CT 201           │  │ CT 202     │ │
│  │ awareness-pg     │  │ awareness-app    │  │ awareness- │ │
│  │ 192.168.200.100  │  │ 192.168.200.101  │  │ tunnel     │ │
│  │                  │  │                  │  │ 192.168.   │ │
│  │ Postgres 17      │  │ mcp-awareness    │  │ 200.102    │ │
│  │ pgvector         │  │ (pip from git)   │  │            │ │
│  │ PostGIS          │  │ systemd service  │  │ cloudflared│ │
│  │ pg_stat_stmts    │  │                  │  │ systemd    │ │
│  │                  │  │ OAuth enabled    │  │            │ │
│  │ :5432 LAN        │  │ :8420            │  │ → CF tunnel│ │
│  └──────────────────┘  └──────────────────┘  └────────────┘ │
│         ↑                    ↑        ↑            │        │
│         │ pg connect         │        │ embed      │        │
│         └────────────────────┘        │            │        │
│                                       │            │        │
│  Ollama (bare metal, 2x P4000)  ◄─────┘            │        │
│  :11434                                             │        │
│                                                     │        │
│  Internet → Cloudflare → staging.mcpawareness.com ──┘        │
│             → CT 202 tunnel → CT 201 awareness               │
└──────────────────────────────────────────────────────────────┘

Synology NAS "Seska" (192.168.200.52)
  └─ /volume1/awareness-backups (encrypted, NFS, 10GB quota)
     ← daily pg_dump from CT 200
```

## CT 200 — Postgres (`awareness-pg`)

### Provisioning
- CT ID: 200
- Hostname: `awareness-pg`
- Template: Debian 12 (already on holodeck)
- Static IP: `192.168.200.100/24`, gateway `192.168.200.1`
- Resources: 2 CPU cores, 2GB RAM, 20GB disk (`local-lvm`)

### Software
- Postgres 17 from PGDG apt repository
- Extensions: `pgvector`, `pg_stat_statements`, `postgis`
- WAL configuration: `wal_level=logical`, `max_replication_slots=4`
- Shared preload libraries: `pg_stat_statements`

### Security
- `pg_hba.conf`: scram-sha-256 for `192.168.200.0/24`
- Dedicated `awareness` database and user
- Strong password (generated, stored in KeePass)

### Backup
- **Daily**: cron job runs `pg_dump -U awareness -Fc awareness > /mnt/backup/awareness-$(date +%F).dump` (custom format is already compressed)
- **Retention**: 30 days, cron prunes older files
- **Destination**: Synology NAS at `192.168.200.52:/volume1/awareness-backups` mounted via NFS at `/mnt/backup`
- **Weekly**: Proxmox CT snapshot (4 retained)

### NFS mount
```fstab
192.168.200.52:/volume1/awareness-backups /mnt/backup nfs rw,hard,intr 0 0
```

## CT 201 — Awareness App (`awareness-app`)

### Provisioning
- CT ID: 201
- Hostname: `awareness-app`
- Template: Debian 12
- Static IP: `192.168.200.101/24`, gateway `192.168.200.1`
- Resources: 1 CPU core, 512MB RAM, 8GB disk (`local-lvm`)

### Software
- Python 3.12 (Debian repos or deadsnakes PPA)
- Clone `cmeans/mcp-awareness` from GitHub
- `pip install -e .` (editable install from source)
- Alembic migrations run on first deploy

### Runtime — systemd service (`mcp-awareness.service`)
```ini
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

[Install]
WantedBy=multi-user.target
```

### Environment (`/etc/awareness/env`)
```bash
AWARENESS_TRANSPORT=streamable-http
AWARENESS_HOST=0.0.0.0
AWARENESS_PORT=8420
AWARENESS_DATABASE_URL=postgresql://awareness:<password>@192.168.200.100:5432/awareness
AWARENESS_AUTH_REQUIRED=true
AWARENESS_JWT_SECRET=<from .env.oauth>
AWARENESS_JWT_ALGORITHM=HS256
AWARENESS_OAUTH_ISSUER=<from .env.oauth>
AWARENESS_OAUTH_AUDIENCE=<from .env.oauth>
AWARENESS_OAUTH_JWKS_URI=<from .env.oauth>
AWARENESS_OAUTH_USER_CLAIM=sub
AWARENESS_OAUTH_AUTO_PROVISION=true
AWARENESS_PUBLIC_URL=https://staging.mcpawareness.com
AWARENESS_DEFAULT_OWNER=admin
AWARENESS_EMBEDDING_PROVIDER=ollama
AWARENESS_EMBEDDING_MODEL=nomic-embed-text
AWARENESS_OLLAMA_URL=http://192.168.200.70:11434
```

### Updates
```bash
cd /opt/mcp-awareness
git pull
pip install -e .
sudo systemctl restart mcp-awareness
```

### User provisioning
Pre-provision Chris's user before first OAuth login:
```bash
mcp-awareness-user add --id "<oauth-sub>" --email <email> --display-name "Chris Means"
```
This tests the flow where OAuth login finds an existing user by email match.

## CT 202 — Tunnel (`awareness-tunnel`)

### Provisioning
- CT ID: 202
- Hostname: `awareness-tunnel`
- Template: Debian 12
- Static IP: `192.168.200.102/24`, gateway `192.168.200.1`
- Resources: 1 CPU core, 256MB RAM, 4GB disk (`local-lvm`)

### Software
- `cloudflared` from Cloudflare's apt repository

### Runtime
- Install as system service: `cloudflared service install`
- Tunnel config points to `http://192.168.200.101:8420`
- Credentials file copied from laptop (`~/.cloudflared/staging-config.yml` and tunnel JSON)

### Tunnel config update
The existing staging tunnel config references `http://awareness-oauth:8421` (Docker network). Must update to:
```yaml
tunnel: <tunnel-id>
credentials-file: /etc/cloudflared/credentials.json
ingress:
  - hostname: staging.mcpawareness.com
    service: http://192.168.200.101:8420
  - service: http_status:404
```

## Data Migration

### Step 0 — Multi-tenant migration on laptop
The laptop's production DB is pre-multi-tenant (no `owner_id`, no `users` table). Must bring it current before dumping.

1. Backup laptop Postgres: `docker exec awareness-postgres pg_dump -U awareness -Fc awareness > ~/awareness-pre-migration.dump`
2. Run migrations: `mcp-awareness-migrate upgrade head` (against laptop Docker Postgres)
3. Verify laptop awareness still works (`get_briefing`, `get_knowledge`)
4. If migration fails, restore from backup

### Step 1 — Dump migrated data
```bash
docker exec awareness-postgres pg_dump -U awareness -Fc awareness > ~/awareness-migrated.dump
```

### Step 2 — Load on holodeck
1. Ensure CT 200 Postgres is running with schema (Alembic migrations already applied)
2. Copy dump to CT 200: `scp ~/awareness-migrated.dump root@192.168.200.100:/tmp/`
3. Restore data: `pg_restore -U awareness -d awareness --data-only /tmp/awareness-migrated.dump`
4. Verify entry count matches

### Step 3 — Verify
1. Start CT 201 (awareness app)
2. Generate a test JWT: `mcp-awareness-token`
3. Verify `get_briefing` returns expected data
4. Start CT 202 (tunnel)
5. Verify `staging.mcpawareness.com` is reachable via OAuth

### Fallback
Laptop's production stack (`mcp.mcpawareness.com`) remains untouched throughout. If holodeck fails, awareness continues on the laptop.

## Host Prerequisites (completed)

- [x] Proxmox host updated (`apt update && apt dist-upgrade`)
- [x] Kernel updated to 6.8.12-20-pve
- [x] NVIDIA driver rebuilt for new kernel (DKMS + headers)
- [x] `nvidia-smi` confirms both P4000s working (driver 535.261.03, CUDA 12.2)
- [x] Ollama running via systemd
- [x] Synology NFS share created (`/volume1/awareness-backups`, encrypted, 10GB quota, checksums, NFS rule for 192.168.200.0/24)

## Load Profiling

Each LXC has cgroup-enforced resource limits that Proxmox tracks. These metrics map directly to cloud instance sizing:

| LXC | Metric | Cloud equivalent |
|-----|--------|-----------------|
| CT 200 (Postgres) | CPU, RAM, disk I/O | RDS/Cloud SQL instance tier |
| CT 201 (Awareness) | CPU, RAM, request rate | Cloud Run instance sizing |
| CT 202 (Tunnel) | CPU, RAM, bandwidth | Cloudflare handles this in cloud (free) |
| Host (Ollama) | GPU util, VRAM, latency | GPU instance or API costs |

Additional metrics to track over time:
- `pg_database_size('awareness')` — storage growth rate
- `pg_stat_statements` — query performance baseline
- Embedding calls/sec — the biggest cloud cost wildcard
- Network egress per CT — Proxmox tracks this natively

## Cloud Migration Path

| Phase | Infrastructure | When |
|-------|---------------|------|
| **Now** | Holodeck LXCs | Immediate |
| **Cloud v1** | Cloud Run + managed Postgres (Cloud SQL / RDS) | When laptop-free operation needed beyond LAN |
| **Cloud v2** | Kubernetes (EKS/GKE) | When service count hits 3+ or paying customers need SLAs |

The Docker image, Postgres config, and load profiling data all carry forward at each phase.

## Network Summary

| Host | IP | Role |
|------|----|------|
| holodeck | 192.168.200.70 | Proxmox host, Ollama bare metal |
| Seska (Synology) | 192.168.200.52 | NAS, backup storage |
| CT 200 | 192.168.200.100 | Postgres |
| CT 201 | 192.168.200.101 | Awareness app |
| CT 202 | 192.168.200.102 | Cloudflare tunnel |
| Laptop | (DHCP) | Fallback production stack |

## Implementation Order

1. Migrate laptop DB to multi-tenant (Step 0) — backup first, then run migrations, verify
2. Provision CT 200 — Postgres with extensions, configure pg_hba, mount NFS backup
3. Run Alembic migrations on CT 200 (creates empty schema)
4. Dump laptop data → restore to CT 200 (data only)
5. Provision CT 201 — Python, clone repo, install, configure env, create systemd service
6. Start CT 201, verify `get_briefing` returns expected data
7. Pre-provision Chris's user via CLI
8. Provision CT 202 — cloudflared, copy credentials, update tunnel config
9. Start CT 202, verify `staging.mcpawareness.com` works via OAuth
10. Set up backup cron in CT 200
11. Set up weekly Proxmox snapshot schedule
