# Data Dictionary

All data in mcp-awareness is stored in a single `entries` table using a common envelope pattern. Every record — whether it's a system status report, an alert, a piece of knowledge, or a preference — shares the same columns. The `type` field determines the semantics, and the `data` column holds type-specific fields.

## Table: `entries`

| Column | Type | Nullable | Description |
|--------|------|----------|-------------|
| `id` | TEXT | No | Primary key. UUID v4, generated via `uuid.uuid4()`. |
| `type` | TEXT | No | Entry type. One of: `status`, `alert`, `pattern`, `suppression`, `context`, `preference`, `note`. |
| `source` | TEXT | No | Origin identifier. Describes the subject, not the owner (e.g., `"personal"`, `"synology-nas"`, `"mcp-awareness-project"`). |
| `created` | TIMESTAMPTZ | No | UTC timestamp. Set once when the entry is first created. |
| `updated` | TIMESTAMPTZ | No | UTC timestamp. Updated on every upsert or `update_entry` call. |
| `expires` | TIMESTAMPTZ | Yes | When set, the entry is eligible for cleanup after this time. `NULL` means permanent (until explicitly deleted). |
| `deleted` | TIMESTAMPTZ | Yes | Timestamp of soft deletion. `NULL` means active. Non-null means trashed — recoverable for 30 days, then auto-purged. |
| `tags` | JSONB | No | Array of strings (e.g., `["infra", "nas", "docker"]`). Used for filtering, suppression matching, and knowledge retrieval. Default: `[]`. |
| `data` | JSONB | No | Object with type-specific fields. Structure depends on `type` — see below. Default: `{}`. |
| `logical_key` | TEXT | Yes | Optional key for upsert deduplication. Unique within a source. |

### Indexes

| Index | Columns | Type | Purpose |
|-------|---------|------|---------|
| `idx_entries_type` | `type` | B-tree | Filter by entry type |
| `idx_entries_source` | `source` | B-tree | Filter by source |
| `idx_entries_type_source` | `type`, `source` | B-tree | Combined filter (e.g., all alerts for a source) |
| `idx_entries_tags_gin` | `tags` | GIN | Fast tag containment queries |
| `idx_entries_source_logical_key` | `source`, `logical_key` | Unique (partial) | Upsert deduplication (WHERE logical_key IS NOT NULL) |

## Entry types

### `status` — System status reports

Written by edge processes via `report_status`. One active entry per source (upserted). If `ttl_sec` elapses without a refresh, the source is marked stale in the briefing.

**`data` fields:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `metrics` | object | Yes | Nested metric groups. Structure is source-defined (e.g., `{"cpu": {"usage_pct": 34}, "memory": {"usage_pct": 71}}`). |
| `inventory` | object | No | Current state of managed resources (e.g., `{"docker": {"running": ["plex"], "stopped": []}}`). |
| `ttl_sec` | integer | Yes | Time-to-live in seconds. If no update arrives within this window, the source is considered stale. Default: 120. |

### `alert` — Active alerts

Written by edge processes via `report_alert`. Keyed by `source` + `alert_id` (upserted). Set `resolved: true` to mark an alert as resolved.

**`data` fields:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `alert_id` | string | Yes | Unique identifier within the source (e.g., `"struct-pihole-stopped"`). Used for upsert matching. |
| `level` | string | Yes | Severity: `"warning"` or `"critical"`. Critical alerts break through warning-level suppressions. |
| `alert_type` | string | Yes | Detection method: `"threshold"`, `"structural"`, or `"baseline"`. |
| `message` | string | Yes | Human-readable alert description. Used in briefing summaries and suppression matching. |
| `resolved` | boolean | Yes | `false` = active, `true` = resolved. Resolved alerts are excluded from the briefing. |
| `details` | object | No | Additional structured context (e.g., affected resources, thresholds). |
| `diagnostics` | object | No | Evidence captured at detection time. Should be recorded when the alert fires — the evidence may be transient. |

### `pattern` — Operational knowledge

Written by agents via `learn_pattern`. Use ONLY for knowledge with conditions and/or effects relevant to the alert collator. For general-purpose knowledge, use `note` instead.

**`data` fields:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `description` | string | Yes | Human-readable description of the operational pattern. |
| `conditions` | object | No | When this pattern applies. Temporal conditions: `{"day_of_week": "friday"}`, `{"hour_range": [2, 6]}`. Default: `{}`. |
| `effect` | string | No | What this pattern implies for alerting (e.g., `"suppress qbittorrent_stopped"`). Used by the collator for pattern-based suppression. Default: `""`. |
| `learned_from` | string | No | Platform that recorded this (e.g., `"claude-code"`, `"claude-ai"`). Default: `"conversation"`. |

### `note` — General-purpose knowledge

Written by agents via `remember`. Permanent unless explicitly deleted. The default choice for storing knowledge — personal facts, project notes, skill backups, config snapshots, anything that doesn't need conditions/effects for alert matching.

**`data` fields:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `description` | string | Yes | Short summary of what this note contains. |
| `content` | string | No | Optional payload — the actual data. Can be plain text, JSON, markdown, etc. |
| `content_type` | string | No | MIME type of the content (e.g., `"text/plain"`, `"application/json"`, `"text/markdown"`). Default: `"text/plain"`. |
| `learned_from` | string | No | Platform that recorded this. Default: `"conversation"`. |
| `changelog` | array | No | Change history. Populated automatically by `update_entry`. Each element: `{"updated": "<timestamp>", "changed": {"<field>": "<old_value>"}}`. |

### `suppression` — Alert suppressions

Written by agents via `suppress_alert`. Time-limited — always has an `expires` timestamp. Suppressions filter alerts out of the briefing. Critical alerts can break through warning-level suppressions via escalation override.

**`data` fields:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `metric` | string | No | Specific metric to suppress (e.g., `"cpu_pct"`). `null` means match by tags/source. |
| `suppress_level` | string | Yes | Maximum alert level to suppress: `"warning"` or `"critical"`. Default: `"warning"`. |
| `escalation_override` | boolean | Yes | If `true`, critical alerts break through even when the suppression matches. Default: `true`. |
| `reason` | string | No | Why the suppression was created. Default: `""`. |

### `context` — Time-limited knowledge

Written by agents via `add_context`. Always has an `expires` timestamp (default: 30 days). Use for events, temporary situations, or facts that lose relevance over time.

**`data` fields:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `description` | string | Yes | Human-readable description of the context. |

### `preference` — User preferences

Written by agents via `set_preference`. Keyed by `key` + `scope` (upserted). Portable across agent platforms.

**`data` fields:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `key` | string | Yes | Preference name (e.g., `"alert_verbosity"`, `"check_frequency"`). |
| `value` | string | Yes | Preference value (e.g., `"one_sentence_warnings"`, `"first_turn_only"`). |
| `scope` | string | Yes | Scope of the preference. Default: `"global"`. |

## Lifecycle

- **Upsert behavior:** `status` entries are upserted by `source`. `alert` entries by `source` + `alert_id`. `preference` entries by `key` + `scope`. Other types always insert new rows.
- **Soft delete:** `delete_entry` sets the `deleted` timestamp. Entry remains in the database for 30 days, recoverable via `restore_entry`. Bulk deletes require `confirm=True` (dry-run by default).
- **Auto-purge:** Expired entries (`expires < now`) and old soft-deleted entries are cleaned up by `_cleanup_expired`, which runs on a background thread triggered by write operations, debounced to at most every 10 seconds. Cleanup never blocks the request. **Note:** auto-purge performs a hard `DELETE`. Expired entries bypass the trash entirely.
- **Staleness:** Status entries with `ttl_sec` are marked stale in the briefing if no update arrives within the TTL window. The entry itself is not deleted.
- **Change tracking:** `update_entry` appends previous field values to the `changelog` array in `data`. Use `get_knowledge(include_history="true")` to see changes, or `include_history="only"` to find entries that have been modified.
- **Hard deletes:** The API only performs soft deletes. Manual SQL `DELETE` statements bypass the trash — that data is gone permanently. Back up regularly.

## Backend details

- **Version:** PostgreSQL 17 recommended (matches RDS support, pgvector 0.8.1)
- **Driver:** psycopg (sync) — matches the synchronous Store protocol
- **Tags/data stored as:** JSONB columns, queried via `jsonb_array_elements_text()` and GIN-indexed `@>` containment
- **GIN index** on `tags` column for fast tag containment queries
- **pgvector extension:** Installed via `pgvector/pgvector:pg17` Docker image. Not yet used — ready for future embedding/RAG support.
- **WAL level:** `wal_level=logical` configured for Debezium CDC readiness and logical replication
- **Replication slots:** `max_replication_slots=4` for future replication/CDC
- **Background cleanup:** Daemon thread with its own psycopg connection, debounced (10s), with alive-check guard to prevent thread accumulation
- **Connection string:** Configured via `AWARENESS_DATABASE_URL` (e.g., `postgresql://user:pass@localhost:5432/awareness`)
- **Docker image:** `pgvector/pgvector:pg17` (PostgreSQL 17 with pgvector pre-installed)
- **Schema migrations:** Managed by Alembic (raw SQL, no ORM). Migration files in `alembic/versions/`. Run `mcp-awareness-migrate` or `alembic upgrade head`. Version tracked in `alembic_version` table.

### RDS compatibility

The PostgreSQL backend is designed for a clean migration path to AWS RDS:

- **All extensions used are RDS-compatible:** pgvector, pg_trgm, btree_gin, pg_cron
- **TimescaleDB intentionally avoided** (not available on RDS due to license restrictions)
- **Logical replication** supported via `wal_level=logical` (RDS parameter group setting)
- **Migration:** `pg_dump` / `pg_restore` with `CREATE EXTENSION` statements
- **No unlogged tables, no large objects** — replication-safe by design
