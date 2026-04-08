<!-- SPDX-License-Identifier: AGPL-3.0-or-later | Copyright (C) 2026 Chris Means -->
# Holodeck Deployment Implementation Plan

> **For agentic workers:** This is an infrastructure provisioning plan, not a code implementation plan. Most steps are shell commands run on remote systems (holodeck, Synology) that the user executes. The agent's role is to guide, verify, and troubleshoot. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deploy MCP Awareness on Proxmox holodeck with 3 LXCs (Postgres, awareness app, cloudflared tunnel), OAuth enabled, daily backups to Synology NAS.

**Architecture:** Three Debian 12 LXCs on Proxmox host holodeck (192.168.200.70). CT 200 runs Postgres 17 with pgvector/PostGIS. CT 201 runs awareness from git via pip/systemd. CT 202 runs cloudflared tunnel. Ollama runs bare metal on the host. Backups go to Synology NAS via NFS.

**Tech Stack:** Proxmox VE 8.4, Debian 12, Postgres 17, pgvector, PostGIS, Python 3.12, cloudflared, NFS, WorkOS AuthKit OAuth

**Spec:** `docs/superpowers/specs/2026-04-01-holodeck-deployment-design.md`

---

## Conventions

- **`[holodeck]`** = SSH session to holodeck (192.168.200.70)
- **`[CT 200]`** = SSH/exec into CT 200 (e.g., `pct enter 200` from holodeck)
- **`[CT 201]`** = SSH/exec into CT 201
- **`[CT 202]`** = SSH/exec into CT 202
- **`[laptop]`** = Commands on Chris's Fedora workstation
- Steps marked **[USER]** require the user to provide secrets, make UI decisions, or run commands on systems the agent can't reach

---

## Task 1: Migrate Laptop DB to Multi-Tenant

The laptop's production Postgres is pre-multi-tenant. Must run Alembic migrations before dumping data.

**Where:** `[laptop]`

- [ ] **Step 1: Backup current database**

```bash
docker exec awareness-postgres pg_dump -U awareness -Fc awareness > ~/awareness-pre-migration.dump
```

Verify the file was created and has reasonable size:
```bash
ls -lh ~/awareness-pre-migration.dump
```
Expected: A file in the range of 100KB–10MB depending on entry count.

- [ ] **Step 2: Check current migration state**

```bash
cd ~/github.com/cmeans/mcp-awareness
AWARENESS_DATABASE_URL="postgresql://awareness:awareness-dev@localhost:5432/awareness" mcp-awareness-migrate current
```
Expected: Shows the current revision. If it's before `f1a2b3c4d5e6` (add owner_id), multi-tenant migrations haven't run.

Note: The laptop Docker Postgres is on port 5432. If it's mapped differently, check with `docker port awareness-postgres`.

- [ ] **Step 3: Run migrations**

```bash
AWARENESS_DATABASE_URL="postgresql://awareness:awareness-dev@localhost:5432/awareness" mcp-awareness-migrate upgrade head
```
Expected: Prints each migration being applied. Should end with the latest revision (`j5e6f7g8h9i0`).

If it fails: restore from backup:
```bash
docker exec -i awareness-postgres pg_restore -U awareness -d awareness --clean --if-exists < ~/awareness-pre-migration.dump
```

- [ ] **Step 4: Verify laptop awareness still works**

Test via the running awareness instance — call `get_briefing` from any connected client.
Expected: Returns the normal briefing with your existing data.

- [ ] **Step 5: Dump migrated database**

```bash
docker exec awareness-postgres pg_dump -U awareness -Fc awareness > ~/awareness-migrated.dump
ls -lh ~/awareness-migrated.dump
```
Expected: File exists, slightly larger than the pre-migration dump (new columns, indexes).

---

## Task 2: Provision CT 200 — Postgres LXC

**Where:** `[holodeck]`

- [ ] **Step 1: Identify the Debian 12 template**

```bash
pveam list local
```
Expected: Shows a `debian-12-standard_*.tar.zst` template. Note the exact filename (e.g., `local:vztmpl/debian-12-standard_12.7-1_amd64.tar.zst`).

- [ ] **Step 2: Create the LXC**

```bash
pct create 200 local:vztmpl/debian-12-standard_12.7-1_amd64.tar.zst \
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
```

**[USER]** Set a root password for the container when prompted. Store in KeePass.

Adjust the template filename if it differs from step 1.

- [ ] **Step 3: Start CT 200**

```bash
pct start 200
pct enter 200
```

- [ ] **Step 4: Update base system**

```bash
apt update && apt upgrade -y
```

- [ ] **Step 5: Install Postgres 17 from PGDG**

```bash
apt install -y curl ca-certificates gnupg
curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc | gpg --dearmor -o /usr/share/keyrings/pgdg.gpg
echo "deb [signed-by=/usr/share/keyrings/pgdg.gpg] http://apt.postgresql.org/pub/repos/apt bookworm-pgdg main" > /etc/apt/sources.list.d/pgdg.list
apt update
apt install -y postgresql-17 postgresql-17-pgvector postgresql-17-postgis-3
```

Expected: Postgres 17 installed, cluster auto-created, service running.

Verify:
```bash
systemctl status postgresql
pg_lsclusters
```
Expected: `17 main` cluster, status `online`.

- [ ] **Step 6: Configure Postgres**

Edit `/etc/postgresql/17/main/postgresql.conf`:
```bash
cat >> /etc/postgresql/17/main/postgresql.conf << 'EOF'

# --- Awareness custom config ---
wal_level = logical
max_replication_slots = 4
shared_preload_libraries = 'pg_stat_statements'
EOF
```

- [ ] **Step 7: Configure authentication**

Edit `/etc/postgresql/17/main/pg_hba.conf` — add this line before any existing `host` rules.
Use `all` for the database field so the `awareness` user can access any database it owns
(e.g., `awareness`, `awareness_sessions`, `postgres` for auto-create):
```bash
echo "host    all    awareness    192.168.200.0/24    scram-sha-256" >> /etc/postgresql/17/main/pg_hba.conf
```

- [ ] **Step 8: Restart Postgres**

```bash
systemctl restart postgresql
systemctl status postgresql
```
Expected: Active (running), no errors.

- [ ] **Step 9: Create database and user**

**[USER]** Generate a strong password in KeePass (32+ chars) for the `awareness` Postgres user.

```bash
sudo -u postgres psql << 'EOF'
CREATE USER awareness WITH PASSWORD '<PASTE_PASSWORD_HERE>' CREATEDB;
CREATE DATABASE awareness OWNER awareness ENCODING 'UTF8' LC_COLLATE 'en_US.UTF-8' LC_CTYPE 'en_US.UTF-8' TEMPLATE template0;
\c awareness
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_stat_statements;
CREATE EXTENSION IF NOT EXISTS postgis;
\dx
EOF
```

Expected: `\dx` shows `vector`, `pg_stat_statements`, and `postgis` extensions.

- [ ] **Step 10: Test remote connectivity (from holodeck host)**

Exit the container first (`exit`), then from holodeck:
```bash
psql -h 192.168.200.100 -U awareness -d awareness -c "SELECT version();"
```
**[USER]** Enter the password when prompted.

Expected: Shows PostgreSQL 17.x version string.

- [ ] **Step 11: Mount NFS backup share**

Back inside CT 200:
```bash
pct enter 200
apt install -y nfs-common
mkdir -p /mnt/backup
```

Add to `/etc/fstab`:
```bash
echo "192.168.200.52:/volume1/awareness-backups /mnt/backup nfs rw,hard,intr 0 0" >> /etc/fstab
```

Mount and test:
```bash
mount /mnt/backup
touch /mnt/backup/test-write && rm /mnt/backup/test-write
echo "NFS mount working"
```
Expected: No errors, file creates and deletes successfully.

---

## Task 3: Run Alembic Migrations on CT 200

This creates the empty schema on the holodeck Postgres. We need a temporary Python environment to run migrations.

**Where:** `[CT 200]`

- [ ] **Step 1: Install Python and git**

```bash
apt install -y python3 python3-pip python3-venv git
```

- [ ] **Step 2: Clone repo and install**

```bash
mkdir -p /opt
cd /opt
git clone https://github.com/cmeans/mcp-awareness.git
cd mcp-awareness
python3 -m venv /opt/mcp-awareness-venv
/opt/mcp-awareness-venv/bin/pip install -e .
```

- [ ] **Step 3: Run migrations**

```bash
AWARENESS_DATABASE_URL="postgresql://awareness:<PASSWORD>@localhost:5432/awareness" \
  /opt/mcp-awareness-venv/bin/mcp-awareness-migrate upgrade head
```

Expected: All migrations apply cleanly, ending at `j5e6f7g8h9i0`.

Verify:
```bash
sudo -u postgres psql -d awareness -c "\dt"
```
Expected: Tables `entries`, `reads`, `actions`, `embeddings`, `users`.

- [ ] **Step 4: Clean up temporary install**

The repo clone in CT 200 was only for migrations. Clean up:
```bash
rm -rf /opt/mcp-awareness /opt/mcp-awareness-venv
apt remove -y python3-pip python3-venv git
apt autoremove -y
```

Note: If future migrations are needed, they'll be run from CT 201 (the app server) pointing at CT 200's Postgres.

---

## Task 4: Restore Data from Laptop

**Where:** `[laptop]` then `[CT 200]`

- [ ] **Step 1: Copy dump to CT 200**

From laptop:
```bash
scp ~/awareness-migrated.dump root@192.168.200.100:/tmp/
```

- [ ] **Step 2: Restore data**

From holodeck (or directly SSH to CT 200):
```bash
pct enter 200
sudo -u postgres pg_restore -U postgres -d awareness --data-only --disable-triggers /tmp/awareness-migrated.dump
```

Note: Using `-U postgres` (superuser) with `--disable-triggers` avoids FK constraint issues during data-only restore. The `awareness` user owns the tables but superuser is needed for trigger manipulation.

If you get errors about duplicate keys (e.g., the `admin` user from migration vs dump), that's expected — the `--data-only` restore may conflict with the default user created by migration `f1a2b3c4d5e6`. Fix with:
```bash
sudo -u postgres psql -d awareness -c "DELETE FROM users;"
sudo -u postgres pg_restore -U postgres -d awareness --data-only --disable-triggers -t entries -t reads -t actions -t embeddings -t users /tmp/awareness-migrated.dump
```

- [ ] **Step 3: Verify entry count**

```bash
sudo -u postgres psql -d awareness -c "SELECT type, count(*) FROM entries WHERE deleted IS NULL GROUP BY type ORDER BY type;"
```

Compare with laptop:
```bash
# On laptop:
docker exec awareness-postgres psql -U awareness -d awareness -c "SELECT type, count(*) FROM entries WHERE deleted IS NULL GROUP BY type ORDER BY type;"
```

Expected: Counts match.

- [ ] **Step 4: Clean up dump file**

```bash
rm /tmp/awareness-migrated.dump
```

---

## Task 5: Provision CT 201 — Awareness App LXC

**Where:** `[holodeck]`

- [ ] **Step 1: Create the LXC**

```bash
pct create 201 local:vztmpl/debian-12-standard_12.7-1_amd64.tar.zst \
  --hostname awareness-app \
  --cores 1 \
  --memory 512 \
  --swap 256 \
  --rootfs local-lvm:8 \
  --net0 name=eth0,bridge=vmbr0,ip=192.168.200.101/24,gw=192.168.200.1 \
  --nameserver 192.168.200.1 \
  --unprivileged 1 \
  --features nesting=0 \
  --start 0 \
  --password
```

**[USER]** Set root password, store in KeePass.

- [ ] **Step 2: Start and enter CT 201**

```bash
pct start 201
pct enter 201
```

- [ ] **Step 3: Update base system**

```bash
apt update && apt upgrade -y
```

- [ ] **Step 4: Install Python 3.12 and dependencies**

Debian 12 ships Python 3.11. For 3.12:
```bash
apt install -y software-properties-common curl git build-essential libpq-dev
```

Check if 3.12 is available:
```bash
apt list python3.12 2>/dev/null
```

If not available, install from deadsnakes or use 3.11 (mcp-awareness requires >=3.10, so 3.11 works fine):
```bash
apt install -y python3 python3-pip python3-venv python3-dev
python3 --version
```

- [ ] **Step 5: Create awareness user**

```bash
useradd --system --create-home --shell /bin/bash awareness
```

- [ ] **Step 6: Clone repo and install**

```bash
mkdir -p /opt/mcp-awareness
chown awareness:awareness /opt/mcp-awareness
sudo -u awareness git clone https://github.com/cmeans/mcp-awareness.git /opt/mcp-awareness
cd /opt/mcp-awareness
sudo -u awareness python3 -m venv /opt/mcp-awareness/venv
sudo -u awareness /opt/mcp-awareness/venv/bin/pip install -e .
```

Verify the CLI is available:
```bash
/opt/mcp-awareness/venv/bin/mcp-awareness --help
```

- [ ] **Step 7: Create environment file**

**[USER]** You'll need values from your laptop's `.env.oauth` file. Don't paste them here — create the file directly on CT 201.

```bash
mkdir -p /etc/awareness
chmod 700 /etc/awareness
cat > /etc/awareness/env << 'EOF'
AWARENESS_TRANSPORT=streamable-http
AWARENESS_HOST=0.0.0.0
AWARENESS_PORT=8420
AWARENESS_DATABASE_URL=postgresql://awareness:<PG_PASSWORD>@192.168.200.100:5432/awareness
AWARENESS_AUTH_REQUIRED=true
AWARENESS_JWT_SECRET=<FROM_.ENV.OAUTH>
AWARENESS_JWT_ALGORITHM=HS256
AWARENESS_OAUTH_ISSUER=<FROM_.ENV.OAUTH>
AWARENESS_OAUTH_AUDIENCE=<FROM_.ENV.OAUTH>
AWARENESS_OAUTH_JWKS_URI=<FROM_.ENV.OAUTH>
AWARENESS_OAUTH_USER_CLAIM=sub
AWARENESS_OAUTH_AUTO_PROVISION=true
AWARENESS_PUBLIC_URL=https://staging.mcpawareness.com
AWARENESS_DEFAULT_OWNER=admin
AWARENESS_EMBEDDING_PROVIDER=ollama
AWARENESS_EMBEDDING_MODEL=nomic-embed-text
AWARENESS_OLLAMA_URL=http://192.168.200.70:11434
EOF
chmod 600 /etc/awareness/env
```

**[USER]** Edit `/etc/awareness/env` and replace all `<...>` placeholders with real values from `.env.oauth`.

- [ ] **Step 8: Create systemd service**

```bash
cat > /etc/systemd/system/mcp-awareness.service << 'EOF'
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
EOF
```

- [ ] **Step 9: Start and verify**

```bash
systemctl daemon-reload
systemctl enable mcp-awareness
systemctl start mcp-awareness
systemctl status mcp-awareness
```

Expected: Active (running).

Check the logs for errors:
```bash
journalctl -u mcp-awareness -n 50 --no-pager
```

Expected: Server starts, connects to Postgres, runs migrations (should be no-op since we already ran them), listens on port 8420.

- [ ] **Step 10: Test connectivity from holodeck host**

Exit the container, from holodeck:
```bash
curl -s http://192.168.200.101:8420/mcp -X POST -H "Content-Type: application/json" -d '{"jsonrpc":"2.0","method":"initialize","params":{"capabilities":{}},"id":1}'
```

Expected: JSON-RPC response (may require auth header — if you get a 401, that means the server is running and OAuth is enforced, which is correct).

---

## Task 6: Pre-provision User via CLI

**Where:** `[CT 201]`

- [ ] **Step 1: Get your OAuth subject ID**

**[USER]** Find your WorkOS `sub` claim. Check your existing `.env.oauth` setup or the WorkOS dashboard for your user's subject identifier. It's typically a `user_` prefixed string.

- [ ] **Step 2: Create the user**

```bash
pct enter 201
sudo -u awareness AWARENESS_DATABASE_URL="$(grep AWARENESS_DATABASE_URL /etc/awareness/env | cut -d= -f2-)" \
  /opt/mcp-awareness/venv/bin/mcp-awareness-user add \
  --id "<YOUR_OAUTH_SUB>" \
  --email "<YOUR_EMAIL>" \
  --display-name "Chris Means"
```

- [ ] **Step 3: Verify user exists**

```bash
sudo -u awareness AWARENESS_DATABASE_URL="$(grep AWARENESS_DATABASE_URL /etc/awareness/env | cut -d= -f2-)" \
  /opt/mcp-awareness/venv/bin/mcp-awareness-user list
```

Expected: Shows the pre-provisioned user.

---

## Task 7: Provision CT 202 — Tunnel LXC

**Where:** `[holodeck]`

- [ ] **Step 1: Create the LXC**

```bash
pct create 202 local:vztmpl/debian-12-standard_12.7-1_amd64.tar.zst \
  --hostname awareness-tunnel \
  --cores 1 \
  --memory 256 \
  --swap 128 \
  --rootfs local-lvm:4 \
  --net0 name=eth0,bridge=vmbr0,ip=192.168.200.102/24,gw=192.168.200.1 \
  --nameserver 192.168.200.1 \
  --unprivileged 1 \
  --features nesting=0 \
  --start 0 \
  --password
```

**[USER]** Set root password, store in KeePass.

- [ ] **Step 2: Start and enter CT 202**

```bash
pct start 202
pct enter 202
```

- [ ] **Step 3: Update and install cloudflared**

```bash
apt update && apt upgrade -y
apt install -y curl
curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg | tee /usr/share/keyrings/cloudflare-main.gpg > /dev/null
echo "deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] https://pkg.cloudflare.com/cloudflared bookworm main" > /etc/apt/sources.list.d/cloudflared.list
apt update
apt install -y cloudflared
cloudflared --version
```

Expected: Shows cloudflared version.

- [ ] **Step 4: Copy tunnel credentials from laptop**

**[USER]** From your laptop, copy the staging tunnel credentials and config to CT 202:

```bash
# From laptop:
scp ~/.cloudflared/staging-config.yml root@192.168.200.102:/etc/cloudflared/config.yml
# Copy the staging tunnel credentials JSON (filename matches your tunnel ID):
scp ~/.cloudflared/<staging-tunnel-id>.json root@192.168.200.102:/etc/cloudflared/credentials.json
```

- [ ] **Step 5: Update tunnel config**

The config currently points to the Docker network name. Update it to point to CT 201:

```bash
pct enter 202
cat > /etc/cloudflared/config.yml << 'EOF'
tunnel: <STAGING_TUNNEL_ID>
credentials-file: /etc/cloudflared/credentials.json
ingress:
  - hostname: staging.mcpawareness.com
    service: http://192.168.200.101:8420
  - service: http_status:404
EOF
```

**[USER]** Replace `<STAGING_TUNNEL_ID>` with your actual staging tunnel ID.

- [ ] **Step 6: Install as system service and start**

```bash
cloudflared service install
systemctl start cloudflared
systemctl status cloudflared
```

Expected: Active (running).

Check logs:
```bash
journalctl -u cloudflared -n 20 --no-pager
```

Expected: `Connection established`, `Registered tunnel connection`.

- [ ] **Step 7: Verify end-to-end OAuth**

**[USER]** From Claude.ai or a browser, connect to `staging.mcpawareness.com`. The OAuth flow should redirect to WorkOS, authenticate, and return awareness data.

Since the user was pre-provisioned in Task 6, the OAuth login should find the existing user by email match rather than auto-provisioning a new one. Verify this by checking the user list again:
```bash
pct enter 201
sudo -u awareness AWARENESS_DATABASE_URL="$(grep AWARENESS_DATABASE_URL /etc/awareness/env | cut -d= -f2-)" \
  /opt/mcp-awareness/venv/bin/mcp-awareness-user list
```

Expected: Same user count as before — no new user was auto-created.

---

## Task 8: Set Up Backup Cron

**Where:** `[CT 200]`

- [ ] **Step 1: Create backup script**

```bash
pct enter 200
cat > /usr/local/bin/awareness-backup.sh << 'SCRIPT'
#!/bin/bash
set -euo pipefail

BACKUP_DIR="/mnt/backup"
RETENTION_DAYS=30
TIMESTAMP=$(date +%F)
DUMP_FILE="${BACKUP_DIR}/awareness-${TIMESTAMP}.dump"

# Check NFS mount
if ! mountpoint -q "${BACKUP_DIR}"; then
    echo "ERROR: ${BACKUP_DIR} is not mounted" >&2
    exit 1
fi

# Dump
sudo -u postgres pg_dump -Fc awareness > "${DUMP_FILE}"

# Verify dump is non-empty
if [ ! -s "${DUMP_FILE}" ]; then
    echo "ERROR: Dump file is empty" >&2
    rm -f "${DUMP_FILE}"
    exit 1
fi

# Prune old backups
find "${BACKUP_DIR}" -name "awareness-*.dump" -mtime +${RETENTION_DAYS} -delete

echo "Backup complete: ${DUMP_FILE} ($(du -h "${DUMP_FILE}" | cut -f1))"
SCRIPT
chmod +x /usr/local/bin/awareness-backup.sh
```

- [ ] **Step 2: Test the backup script**

```bash
/usr/local/bin/awareness-backup.sh
ls -lh /mnt/backup/
```

Expected: Shows `awareness-2026-04-01.dump` (or today's date) with non-zero size.

- [ ] **Step 3: Add cron job**

```bash
crontab -e
```

Add this line (runs daily at 3:00 AM):
```
0 3 * * * /usr/local/bin/awareness-backup.sh >> /var/log/awareness-backup.log 2>&1
```

Verify:
```bash
crontab -l
```

Expected: Shows the backup cron entry.

---

## Task 9: Set Up Proxmox Snapshot Schedule

**Where:** `[holodeck]` (Proxmox host, not inside a container)

- [ ] **Step 1: Create weekly snapshot job via Proxmox UI or CLI**

Option A — Proxmox web UI:
1. Go to Datacenter → Backup
2. Add a new backup job
3. Selection: CT 200, CT 201, CT 202
4. Schedule: Weekly (e.g., Sunday 4:00 AM)
5. Storage: local
6. Mode: Snapshot
7. Retention: 4 (keep last 4 weekly backups)

Option B — CLI:
```bash
# Individual snapshots (run from holodeck host)
for ct in 200 201 202; do
  pct snapshot $ct weekly-$(date +%F) --description "Weekly automated snapshot"
done
```

For automated weekly CLI snapshots, create a cron on the holodeck host:
```bash
cat > /usr/local/bin/awareness-snapshots.sh << 'SCRIPT'
#!/bin/bash
set -euo pipefail
for ct in 200 201 202; do
  # Create new snapshot
  pct snapshot $ct weekly-$(date +%F) --description "Weekly automated snapshot"
  # Prune: keep only last 4 weekly snapshots
  pct listsnapshot $ct | grep "weekly-" | head -n -4 | awk '{print $2}' | while read snap; do
    pct delsnapshot $ct "$snap"
  done
done
SCRIPT
chmod +x /usr/local/bin/awareness-snapshots.sh
```

Add to holodeck cron (runs Sunday 4:00 AM):
```bash
echo "0 4 * * 0 /usr/local/bin/awareness-snapshots.sh >> /var/log/awareness-snapshots.log 2>&1" >> /var/spool/cron/crontabs/root
```

The Proxmox UI approach (Option A) is recommended — it handles retention automatically and shows backups in the UI.

---

## Task 10: Verification Checklist

Final end-to-end verification after all tasks complete.

- [ ] **CT 200 (Postgres)**
  - `systemctl status postgresql` → active
  - `psql -h 192.168.200.100 -U awareness -d awareness -c "\dx"` → shows vector, postgis, pg_stat_statements
  - NFS mount: `mountpoint /mnt/backup` → is a mountpoint
  - Backup file exists in `/mnt/backup/`

- [ ] **CT 201 (Awareness)**
  - `systemctl status mcp-awareness` → active
  - `journalctl -u mcp-awareness -n 5` → no errors
  - Port open: `curl -s http://192.168.200.101:8420/mcp` → responds (401 expected without auth)

- [ ] **CT 202 (Tunnel)**
  - `systemctl status cloudflared` → active
  - `journalctl -u cloudflared -n 5` → connection established

- [ ] **Ollama connectivity**
  - From CT 201: `curl -s http://192.168.200.70:11434/api/tags` → returns model list

- [ ] **OAuth end-to-end**
  - Connect to `staging.mcpawareness.com` from Claude.ai
  - WorkOS login completes
  - `get_briefing` returns data
  - User matched by email (not auto-provisioned)

- [ ] **Laptop fallback**
  - `mcp.mcpawareness.com` still works from Claude Code
  - Laptop Docker stack untouched

- [ ] **Auto-start on reboot**
  - All 3 CTs set to start on boot:
    ```bash
    # From holodeck:
    pct set 200 --onboot 1
    pct set 201 --onboot 1
    pct set 202 --onboot 1
    ```
  - CT boot order: 200 first (Postgres), then 201 (app), then 202 (tunnel):
    ```bash
    pct set 200 --startup order=1
    pct set 201 --startup order=2,up=15
    pct set 202 --startup order=3,up=5
    ```
    (up=15 gives Postgres 15 seconds to start before awareness launches; up=5 gives awareness 5 seconds before tunnel)
