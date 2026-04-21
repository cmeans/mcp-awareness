<!-- SPDX-License-Identifier: AGPL-3.0-or-later | Copyright (C) 2026 Chris Means -->
# Backup and restore

`mcp-awareness` persists **your** data — knowledge, intentions, alerts, preferences, embeddings. A backup strategy is not optional for a persistent-memory product. This guide covers the minimum that every self-hoster should have running within a day of first deploy.

The default Docker Compose stack binds the Postgres data directory to `${AWARENESS_PG_DATA:-~/awareness-pg}` on the host. That means **if your host disk survives, your data survives** — but "the host disk survives" is a strong assumption you should not rely on. Off-host backups are the real protection.

## What to back up

| Table / object | Back it up? | Why |
|----------------|-------------|-----|
| `entries` | **Yes** | The store itself — knowledge, status, alerts, intentions, preferences, records. Irreplaceable. |
| `embeddings` | Optional | Can be regenerated from `entries` via `backfill_embeddings`. Skipping makes backups much smaller, at the cost of a few minutes of CPU after restore. |
| `alembic_version` | **Yes** | Tells a fresh Postgres which migrations are already applied. Restoring entries without this breaks `mcp-awareness-migrate`. |
| `pg_ts_config` (system catalog) | No | Postgres-managed. Recreated by the image. |
| Docker volume files (`~/awareness-pg/*`) | Alternative | A full volume snapshot is equivalent to a full dump — same fidelity, harder to transport between Postgres versions. |

**In-flight state you don't back up** but should know about:

- The Postgres connection pool, the embedding background thread, and the cleanup daemon are all process-local. They rebuild on container restart.
- OAuth/JWT tokens in `docker-compose.yaml`'s environment variables live in `.env`, not Postgres — back up `.env` separately (see [§.env and secrets](#env-and-secrets) below).

## The minimum: a daily logical dump

This is the smallest useful backup strategy. Runs `pg_dump` inside the Postgres container and writes a compressed SQL file to a directory you control, with a timestamped filename. Skips the `embeddings` table for size.

```bash
# From the directory containing docker-compose.yaml.
# Adjust BACKUP_DIR to a path on a different filesystem or machine.
BACKUP_DIR=~/backups/mcp-awareness
mkdir -p "$BACKUP_DIR"
TS=$(date -u +%Y%m%dT%H%M%SZ)
docker compose exec -T postgres pg_dump \
  --username=awareness \
  --dbname=awareness \
  --exclude-table-data=embeddings \
  --format=custom --compress=9 \
  > "$BACKUP_DIR/awareness-$TS.dump"
```

Notes on the flags:

- `--exclude-table-data=embeddings` keeps the **schema** for the `embeddings` table (so restore works) but skips the **rows**. After restore, run `backfill_embeddings` to regenerate them.
- `--format=custom` (binary, compressed, selective restore possible) is preferred over plain SQL for anything larger than a few megabytes.
- `--compress=9` trades CPU for size. Drop to `--compress=5` if your Postgres host is CPU-bound during dumps.

**If you want the absolute-fidelity dump** including embeddings — useful when moving between hosts and you'd rather not wait for backfill:

```bash
docker compose exec -T postgres pg_dump \
  --username=awareness \
  --dbname=awareness \
  --format=custom --compress=9 \
  > "$BACKUP_DIR/awareness-full-$TS.dump"
```

The full dump is typically 3–10× the size of the without-embeddings variant, depending on how many entries you've accumulated.

## Restore

Restoring is more involved than backup because of migration state. The safe path:

```bash
# 1. Stop the application so no writes happen during restore.
docker compose stop mcp-awareness

# 2. Drop and recreate the database inside the running Postgres container.
#    This wipes everything — make sure you have a backup of the current
#    state first if it matters.
docker compose exec -T postgres psql --username=awareness --dbname=postgres <<'SQL'
DROP DATABASE IF EXISTS awareness;
CREATE DATABASE awareness OWNER awareness;
SQL

# 3. Restore the dump. pg_restore (not psql) reads the --format=custom file.
docker compose exec -T postgres pg_restore \
  --username=awareness \
  --dbname=awareness \
  --no-owner --no-acl \
  < ~/backups/mcp-awareness/awareness-20260420T120000Z.dump

# 4. Start the application.
docker compose start mcp-awareness

# 5. If the dump excluded embeddings, regenerate them.
#    The server will run this in the background once called; check status via get_stats.
#    Alternatively, invoke from any MCP client connected to the server:
#      backfill_embeddings()
```

A few things go wrong in practice:

- **`mcp-awareness-migrate` complains about schema drift on first startup.** The `alembic_version` row in the dump must match the code version being deployed. If you restored a dump from v0.17 but the image is v0.18, run `mcp-awareness-migrate upgrade head` inside the `mcp-awareness` container after restore.
- **Dump taken during an active write.** `pg_dump` uses a consistent snapshot by default, but if you used any of the `--jobs` parallelism flags without also using `--serializable-deferrable` you may get an inconsistent snapshot. The single-process form shown above is safe; don't add `--jobs` to a backup script unless you understand the implications.
- **Postgres major version mismatch.** `pg_dump` from PG 17 restores cleanly into PG 17 or later. It does **not** reliably restore into PG 16. If you're rolling back a Postgres version, you need the exact source version.

## Frequency

These are starting points, not prescriptions — pick a cadence that matches how much data you're willing to lose.

| Use case | Cadence | Retention |
|----------|---------|-----------|
| Personal single-user deployment | Daily | 14 days on-host + 30 days off-host |
| Heavy personal use (rapid project iteration, multiple edge providers writing) | Hourly during work hours | 7 days hourly + 30 days daily |
| Small-team or shared deployment | Hourly | 48 hours hourly + 90 days daily |
| Production managed deployment | Continuous WAL archiving (beyond the scope of this doc) | Per your compliance needs |

**Off-host storage is the real protection.** A backup on the same machine as Postgres is a backup of "yesterday's snapshot if someone accidentally runs `DROP TABLE entries`"; it is not a backup against disk failure, ransomware, or the machine catching fire. At minimum, `rsync` / `rclone` / S3-push the dump directory to a different device (NAS, cloud bucket, another host) on the same schedule as the dump runs.

## The backup script

Both the cron and systemd examples below call the same small shell script, so you only write the logic once. Save this as `~/bin/mcp-awareness-backup.sh` (or wherever you keep local scripts), make it executable, and point your scheduler at it.

```bash
#!/bin/bash
# ~/bin/mcp-awareness-backup.sh — daily pg_dump of mcp-awareness
#
# Runs as the user whose ~/backups/mcp-awareness directory holds the
# dumps. Skips the embeddings table for size (regenerate with
# backfill_embeddings after restore). Keeps 14 days of history.
set -euo pipefail

BACKUP_DIR="${BACKUP_DIR:-$HOME/backups/mcp-awareness}"
COMPOSE_DIR="${COMPOSE_DIR:-$HOME/mcp-awareness}"   # where docker-compose.yaml lives

mkdir -p "$BACKUP_DIR"
TS=$(date -u +%Y%m%dT%H%M%SZ)

cd "$COMPOSE_DIR"
docker compose exec -T postgres pg_dump \
  --username=awareness --dbname=awareness \
  --exclude-table-data=embeddings \
  --format=custom --compress=9 \
  > "$BACKUP_DIR/awareness-$TS.dump"

# Rotate: delete dumps older than 14 days.
find "$BACKUP_DIR" -name 'awareness-*.dump' -mtime +14 -delete
```

Make executable and test by hand before wiring up a scheduler:

```bash
chmod +x ~/bin/mcp-awareness-backup.sh
~/bin/mcp-awareness-backup.sh
ls -la ~/backups/mcp-awareness/
```

Putting the logic in a script (instead of inline in the crontab or systemd unit) avoids two classes of bugs that bit earlier drafts of this doc: cron's interpretation of `%` as end-of-command, and systemd's `%`-specifier substitution inside `ExecStart=`. The script owns the logic; schedulers just invoke it.

## Sample cron job

Once `~/bin/mcp-awareness-backup.sh` exists and runs manually, `crontab -e` as the user who owns it:

```
# Daily at 03:15 local time — pg_dump excluding embeddings, retain 14 days.
15 3 * * * /home/youruser/bin/mcp-awareness-backup.sh
```

Replace `youruser` with your actual Unix username. A single physical line is important — cron treats each non-blank line as a separate entry, so backslash-continuations across multiple lines do not work and silently leave broken entries behind.

## Sample systemd timer

If you prefer systemd to cron, two unit files on the Postgres host. **Replace `youruser` with your actual Unix username** in both files — these are literal placeholders, not template specifiers.

```ini
# /etc/systemd/system/mcp-awareness-backup.service
[Unit]
Description=Daily pg_dump of mcp-awareness
After=docker.service

[Service]
Type=oneshot
User=youruser
ExecStart=/home/youruser/bin/mcp-awareness-backup.sh
```

```ini
# /etc/systemd/system/mcp-awareness-backup.timer
[Unit]
Description=Run mcp-awareness-backup daily

[Timer]
OnCalendar=*-*-* 03:15:00
Persistent=true
Unit=mcp-awareness-backup.service

[Install]
WantedBy=timers.target
```

Enable:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now mcp-awareness-backup.timer
systemctl list-timers mcp-awareness-backup.timer
```

A note on why this uses a literal `User=youruser` instead of `User=%i`: the `%i` specifier is the *instance* specifier, which only resolves inside template units (files ending in `@.service`). The service file above is not a template, so `%i` would resolve to an empty string and `User=` would become invalid. If you want a template, rename the file to `mcp-awareness-backup@.service`, use `User=%i`, and enable as `mcp-awareness-backup@youruser.service` — but the single-user form above is simpler for a personal deployment.

## `.env` and secrets

Don't back up `.env` into the same untrusted directory as the database dump. Keep it in a password manager or secrets vault (Bitwarden, 1Password, age-encrypted file in a private repo, HashiCorp Vault — any of these are fine). Secrets to preserve:

- `POSTGRES_PASSWORD`
- `AWARENESS_MOUNT_PATH` (the secret URL path)
- Any JWT signing secret / OAuth client credentials configured per `docs/auth-setup.md`
- `~/.cloudflared/` if you run a named tunnel (the tunnel credentials, not the config)

Without these, a restored Postgres dump is not reachable by the application even if the data itself is intact.

## Practice the restore

Backups that have never been restored are hopes, not backups. Schedule a restore drill at least once per quarter.

The drill uses the **production compose file** (`docker-compose.yaml`) with two env-var overrides: the host-side port (so the drill doesn't collide with the running production instance) and the Postgres data directory (so the drill's Postgres data stays isolated from production's bind mount).

**The drill requires production to be briefly stopped.** Production and drill share the same `container_name` values in `docker-compose.yaml`, so they can't run concurrently against each other. For a single-maintainer deployment this is fine: stop production, spin up the drill, verify, tear down the drill, restart production. Plan for ~10-15 minutes of downtime per drill.

Drill procedure:

1. Stop production:
   ```bash
   docker compose down
   ```
2. Bring the drill stack up on a different host port, with a separate Postgres data directory:
   ```bash
   export AWARENESS_PORT=8421
   export AWARENESS_PG_DATA=~/awareness-pg-drill
   docker compose up -d
   ```
3. Restore the most recent dump into the drill's Postgres. §Restore's commands work as-written — no substitutions, same service names, same database name:
   ```bash
   docker compose stop mcp-awareness
   docker compose exec -T postgres psql --username=awareness --dbname=postgres <<'SQL'
   DROP DATABASE IF EXISTS awareness;
   CREATE DATABASE awareness OWNER awareness;
   SQL
   docker compose exec -T postgres pg_restore \
     --username=awareness --dbname=awareness \
     --no-owner --no-acl \
     < ~/backups/mcp-awareness/awareness-*.dump
   docker compose start mcp-awareness
   ```
4. Connect an MCP client to the drill instance on `http://127.0.0.1:8421/mcp` and call `get_stats()` and `get_briefing()`. Compare the counts to what the most recent production dump reported — the drill should restore every row that made it into the backup.
5. Tear the drill stack down and remove the drill data directory so the next drill starts from a clean slate:
   ```bash
   docker compose down
   rm -rf ~/awareness-pg-drill
   unset AWARENESS_PORT AWARENESS_PG_DATA
   ```
6. Bring production back up:
   ```bash
   docker compose up -d
   ```

If step 4 surprises you (counts don't match, briefing is empty, restore hit unexpected errors), the instructions above drifted from reality — file an issue, fix the doc, try again. The point of a drill is to find the drift before you need the dump for real.

**Why the brief production downtime?** `docker-compose.yaml` sets fixed `container_name:` values (`mcp-awareness`, `awareness-postgres`, etc.) — Docker Compose uses those literal names regardless of project (`-p`) flag. Running a parallel drill stack against the same compose file would collide on those names. Aligning/parameterizing the container names to allow concurrent drills is a possible future enhancement; for a single-maintainer deployment, a 10-minute planned downtime per quarter is cheaper than the config complexity.

**For concurrent-with-production drills** (CI, automated integration tests, or future multi-user deployments that can't easily stop production), use `docker-compose.qa.yaml` — it exposes `127.0.0.1:8421:8420`, uses `-qa`-suffixed service/database names, and isolates data in a named volume. The `-qa` name suffixes mean the §Restore commands need substitutions (`postgres` → `postgres-qa`, `mcp-awareness` → `mcp-awareness-qa`, `--dbname=awareness` → `--dbname=awareness_qa`) — so the production-compose-file pattern above is simpler and is preferred for the quarterly drill.

## Related

- **Export CLI** ([#223](https://github.com/cmeans/mcp-awareness/issues/223), planned) — user-portable JSON export for "my data goes with me" portability. Different audience: `pg_dump` is operator-level full fidelity (schema + state + migration version); the export CLI targets a human-readable data take-out.
- **Embedding regeneration** — the `backfill_embeddings` MCP tool recomputes embeddings for any rows missing them. Called automatically after a restore that skipped embeddings, or manually from any connected MCP client.
- **Migration management** — `mcp-awareness-migrate` is the Alembic CLI. `mcp-awareness-migrate current` shows the applied version; `mcp-awareness-migrate upgrade head` applies pending migrations. Run after a restore from an older version's dump.
