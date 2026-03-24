# Data Dictionary

All data in mcp-awareness is stored in a single `entries` table using a common envelope pattern. Every record — whether it's a system status report, an alert, a piece of knowledge, or a preference — shares the same columns. The `type` field determines the semantics, and the `data` column holds type-specific fields.

## Table: `entries`

| Column | Type | Nullable | Description |
|--------|------|----------|-------------|
| `id` | TEXT | No | Primary key. UUID v4, generated via `uuid.uuid4()`. |
| `type` | TEXT | No | Entry type. One of: `status`, `alert`, `pattern`, `suppression`, `context`, `preference`, `note`, `intention`. |
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

### `intention` — Goals with conditions

Written by agents via `remind`. Intentions have a lifecycle: they start `pending`, fire when conditions are met (currently time-based via `deliver_at`), and complete when the user acts on them. The collator evaluates pending intentions during briefing generation.

**`data` fields:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `goal` | string | Yes | What outcome is desired (e.g., "pick up milk", "tell Mom about insurance"). |
| `state` | string | Yes | Lifecycle state: `pending`, `fired`, `completed`, `snoozed`, `cancelled`. |
| `deliver_at` | string | No | ISO 8601 timestamp — when to surface this intention. Required for time-based triggers. |
| `constraints` | string | No | Preferences or requirements (e.g., "organic, budget-conscious"). |
| `urgency` | string | No | `"low"`, `"normal"`, or `"high"`. Default: `"normal"`. |
| `recurrence` | string | No | Reserved for future use. Currently only one-shot (`null`) is supported. |
| `state_reason` | string | No | Explanation for the current state (e.g., "completed at Mariano's", "not today"). |
| `learned_from` | string | No | Platform that created this. Default: `"conversation"`. |
| `changelog` | array | No | State transition history. Each element: `{"updated": "<timestamp>", "changed": {"state": "<old_state>"}}`. |

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
- **Read/action cleanup:** The `reads` and `actions` tables use `ON DELETE CASCADE` on `entry_id`. This means read and action records are automatically removed when an entry is **hard deleted** (auto-purge or manual SQL). Soft delete (`delete_entry`) does **not** cascade — reads and actions persist for trashed entries until the 30-day purge.

## Table: `reads`

Auto-populated when entries are accessed via `get_knowledge` and `get_alerts`. Fire-and-forget — read log failures never block tool responses.

| Column | Type | Nullable | Description |
|--------|------|----------|-------------|
| `id` | SERIAL | No | Auto-incrementing primary key. |
| `entry_id` | TEXT | No | References `entries(id)` with `ON DELETE CASCADE`. |
| `timestamp` | TIMESTAMPTZ | No | When the read occurred. Default: `now()`. |
| `platform` | TEXT | Yes | Which platform performed the read (e.g., `"claude-code"`). |
| `tool_used` | TEXT | Yes | Which tool triggered the read (e.g., `"get_knowledge"`). |

### Indexes

| Index | Columns | Type | Purpose |
|-------|---------|------|---------|
| `idx_reads_entry` | `entry_id` | B-tree | Look up reads for a specific entry |
| `idx_reads_timestamp` | `timestamp` | B-tree | Time-range queries |

## Table: `actions`

Agent-reported records of concrete actions taken because of an entry. Permanent audit trail.

| Column | Type | Nullable | Description |
|--------|------|----------|-------------|
| `id` | SERIAL | No | Auto-incrementing primary key. |
| `entry_id` | TEXT | No | References `entries(id)` with `ON DELETE CASCADE`. |
| `timestamp` | TIMESTAMPTZ | No | When the action was recorded. Default: `now()`. |
| `platform` | TEXT | Yes | Which platform reported the action (e.g., `"claude-code"`). |
| `action` | TEXT | No | What was done (e.g., `"created GitHub issue #42"`). |
| `detail` | TEXT | Yes | Optional structured reference (PR URL, issue number, etc.). |
| `tags` | JSONB | No | Tags for filtered queries. Default: copied from referenced entry. |

### Indexes

| Index | Columns | Type | Purpose |
|-------|---------|------|---------|
| `idx_actions_entry` | `entry_id` | B-tree | Look up actions for a specific entry |
| `idx_actions_timestamp` | `timestamp` | B-tree | Time-range queries |
| `idx_actions_tags_gin` | `tags` | GIN | Fast tag containment queries |

## Table: `embeddings`

Stores vector embeddings for semantic search. One embedding per entry per model.

| Column | Type | Nullable | Default | Description |
|--------|------|----------|---------|-------------|
| `id` | `SERIAL` | NO | auto | Row ID |
| `entry_id` | `TEXT` | NO | — | FK → `entries.id` (`ON DELETE CASCADE`) |
| `model` | `TEXT` | NO | — | Embedding model name (e.g., `nomic-embed-text`) |
| `dimensions` | `INTEGER` | NO | — | Vector dimension count (e.g., 768) |
| `text_hash` | `TEXT` | NO | — | SHA-256 of embedded text (staleness detection) |
| `embedding` | `VECTOR(768)` | NO | — | pgvector embedding |
| `created` | `TIMESTAMPTZ` | NO | `now()` | When this embedding was generated |

**Constraints:** `UNIQUE (entry_id, model)` — one embedding per entry per model, upsert on conflict.

### Indexes

| Index | Columns | Type | Purpose |
|-------|---------|------|---------|
| `idx_embeddings_entry` | `entry_id` | B-tree | Look up embeddings for a specific entry |
| `idx_embeddings_vector_hnsw` | `embedding` | HNSW (`vector_cosine_ops`) | Fast approximate nearest neighbor search |

### Notes

- Embeddings are generated fire-and-forget on write (never blocks the response)
- Suppression entries are not embedded (short-lived, not worth searching)
- The `text_hash` column enables detection of stale embeddings after entry updates
- `ON DELETE CASCADE` ensures embeddings are cleaned up when entries are deleted
- Requires `AWARENESS_EMBEDDING_PROVIDER=ollama` to activate (optional)
- **Dimension constraint**: `VECTOR(768)` is hardcoded in both inline DDL and Alembic migration, matching `nomic-embed-text`. To use a model with different dimensions, both the DDL and migration must be updated. `AWARENESS_EMBEDDING_DIMENSIONS` configures the provider but does not alter the column type.

## Conventions

### Entry relationships (`related_ids`)

Entries can cross-reference each other via an optional `related_ids` field in the `data` JSONB column:

```json
{
  "description": "Decided to go Postgres-only",
  "related_ids": ["abc-123", "def-456"]
}
```

This is a convention, not a schema constraint — no migration needed. The `get_related` tool traverses relationships bidirectionally:
- **Forward**: entries this entry references (IDs in `related_ids`)
- **Reverse**: entries that reference this entry (via `data->'related_ids' @> '["entry-id"]'::jsonb`)

Use cases: decision → context, intention → action, note → note ("see also").

## Backend details

- **Version:** PostgreSQL 17 recommended (matches RDS support, pgvector 0.8.1)
- **Driver:** psycopg (sync) — matches the synchronous Store protocol. Auto-healing connection property with 30s health check debounce — dead connections reconnect transparently after Postgres restarts.
- **Tags/data stored as:** JSONB columns, queried via `jsonb_array_elements_text()` and GIN-indexed `@>` containment
- **GIN index** on `tags` column for fast tag containment queries
- **pgvector extension:** Installed via `pgvector/pgvector:pg17` Docker image. Used by the `embeddings` table for HNSW vector similarity search.
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
