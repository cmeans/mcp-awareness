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

## Sample cron job

`crontab -e` as the user whose `~/backups/mcp-awareness` directory holds the dumps (not as root unless the directory lives in `/root`):

```cron
# Daily at 03:15 local time — pg_dump excluding embeddings, retain 14 days.
15 3 * * * cd /path/to/mcp-awareness && \
  BACKUP_DIR=$HOME/backups/mcp-awareness && \
  TS=$(date -u +\%Y\%m\%dT\%H\%M\%SZ) && \
  docker compose exec -T postgres pg_dump --username=awareness --dbname=awareness --exclude-table-data=embeddings --format=custom --compress=9 > $BACKUP_DIR/awareness-$TS.dump && \
  find $BACKUP_DIR -name 'awareness-*.dump' -mtime +14 -delete
```

The `\%` escapes are required: cron interprets `%` as end-of-command inside the job line, which would silently truncate everything after `+%Y`.

## Sample systemd timer

If you prefer systemd to cron, two unit files on the Postgres host:

```ini
# /etc/systemd/system/mcp-awareness-backup.service
[Unit]
Description=Daily pg_dump of mcp-awareness
After=docker.service

[Service]
Type=oneshot
User=%i
WorkingDirectory=/path/to/mcp-awareness
Environment=BACKUP_DIR=%h/backups/mcp-awareness
ExecStart=/bin/sh -c 'mkdir -p "$BACKUP_DIR" && TS=$(date -u +%%Y%%m%%dT%%H%%M%%SZ) && docker compose exec -T postgres pg_dump --username=awareness --dbname=awareness --exclude-table-data=embeddings --format=custom --compress=9 > "$BACKUP_DIR/awareness-$TS.dump" && find "$BACKUP_DIR" -name "awareness-*.dump" -mtime +14 -delete'
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

## `.env` and secrets

Don't back up `.env` into the same untrusted directory as the database dump. Keep it in a password manager or secrets vault (Bitwarden, 1Password, age-encrypted file in a private repo, HashiCorp Vault — any of these are fine). Secrets to preserve:

- `POSTGRES_PASSWORD`
- `AWARENESS_MOUNT_PATH` (the secret URL path)
- Any JWT signing secret / OAuth client credentials configured per `docs/auth-setup.md`
- `~/.cloudflared/` if you run a named tunnel (the tunnel credentials, not the config)

Without these, a restored Postgres dump is not reachable by the application even if the data itself is intact.

## Practice the restore

Backups that have never been restored are hopes, not backups. Schedule a restore drill at least once per quarter:

1. Spin up a second Docker Compose stack on a different port (e.g., set `AWARENESS_PORT=8421` and `AWARENESS_PG_DATA=~/awareness-pg-drill`).
2. Restore the most recent dump into it following the §Restore section.
3. Connect an MCP client to the drill instance and call `get_stats()` and `get_briefing()`. Compare the counts to what the production instance reports at the same moment.
4. Tear down the drill stack.

If step 3 surprises you (counts don't match, briefing is empty, restore hit unexpected errors), the instructions above drifted from reality — file an issue, fix the doc, try again. The point of a drill is to find the drift before you need the dump for real.

## Related

- **Export CLI** ([#223](https://github.com/cmeans/mcp-awareness/issues/223), planned) — user-portable JSON export for "my data goes with me" portability. Different audience: `pg_dump` is operator-level full fidelity (schema + state + migration version); the export CLI targets a human-readable data take-out.
- **Embedding regeneration** — the `backfill_embeddings` MCP tool recomputes embeddings for any rows missing them. Called automatically after a restore that skipped embeddings, or manually from any connected MCP client.
- **Migration management** — `mcp-awareness-migrate` is the Alembic CLI. `mcp-awareness-migrate current` shows the applied version; `mcp-awareness-migrate upgrade head` applies pending migrations. Run after a restore from an older version's dump.
