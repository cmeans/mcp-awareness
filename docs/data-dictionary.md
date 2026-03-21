# Data Dictionary

All data in mcp-awareness is stored in a single SQLite table using a common envelope pattern. Every record — whether it's a system status report, an alert, a piece of knowledge, or a preference — shares the same columns. The `type` field determines the semantics, and the `data` column holds type-specific JSON.

## Table: `entries`

| Column | Type | Nullable | Description |
|--------|------|----------|-------------|
| `id` | TEXT | No | Primary key. UUID v4, generated via `uuid.uuid4()`. |
| `type` | TEXT | No | Entry type. One of: `status`, `alert`, `pattern`, `suppression`, `context`, `preference`. |
| `source` | TEXT | No | Origin identifier. Free-form string chosen by the writer (e.g., `"home-nas"`, `"user-preferences"`, `"family"`). Used for grouping and filtering. |
| `created` | TEXT | No | ISO 8601 UTC timestamp. Set once when the entry is first created. |
| `updated` | TEXT | No | ISO 8601 UTC timestamp. Updated on every upsert. |
| `expires` | TEXT | Yes | ISO 8601 UTC timestamp. When set, the entry is eligible for cleanup after this time. `NULL` means permanent (until explicitly deleted). |
| `deleted` | TEXT | Yes | ISO 8601 UTC timestamp of soft deletion. `NULL` means active. Non-null means trashed — recoverable for 30 days, then auto-purged. |
| `tags` | TEXT | No | JSON array of strings (e.g., `["infra", "nas", "docker"]`). Used for filtering, suppression matching, and knowledge retrieval. Default: `[]`. |
| `data` | TEXT | No | JSON object with type-specific fields. Structure depends on `type` — see below. Default: `{}`. |

### Indexes

| Index | Columns | Purpose |
|-------|---------|---------|
| `idx_entries_type` | `type` | Filter by entry type |
| `idx_entries_source` | `source` | Filter by source |
| `idx_entries_type_source` | `type`, `source` | Combined filter (e.g., all alerts for a source) |

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
| `alert_type` | string | Yes | Detection method: `"threshold"` (metric exceeded limit), `"structural"` (expected process/state missing), or `"baseline"` (deviation from normal — planned). |
| `message` | string | Yes | Human-readable alert description. Used in briefing summaries and suppression matching. |
| `resolved` | boolean | Yes | `false` = active, `true` = resolved. Resolved alerts are excluded from the briefing. |
| `details` | object | No | Additional structured context (e.g., affected resources, thresholds). |
| `diagnostics` | object | No | Evidence captured at detection time (e.g., top processes, I/O stats). Should be recorded when the alert fires — the evidence may be transient. |

### `pattern` — Permanent knowledge

Written by agents via `learn_pattern`. Permanent unless explicitly deleted. This is the primary knowledge store — personal facts, system behavior, preferences, anything worth remembering across agent platforms.

**`data` fields:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `description` | string | Yes | Human-readable description of the knowledge (e.g., `"Home network runs Ubiquiti UniFi. VLAN 10 is IoT, VLAN 20 is trusted."`). |
| `conditions` | object | No | When this knowledge applies. Can include temporal conditions like `{"day_of_week": "friday"}` or `{"hour_range": [2, 6]}`. Default: `{}`. |
| `effect` | string | No | For operational patterns: what this knowledge implies for alerting (e.g., `"suppress qbittorrent_stopped"`). Used by the collator for pattern-based suppression. Default: `""`. |
| `learned_from` | string | No | Where this knowledge was recorded. Should identify the platform (e.g., `"claude.ai"`, `"claude-code"`, `"claude-desktop"`, `"conversation"`). Default: `"conversation"`. |

### `suppression` — Alert suppressions

Written by agents via `suppress_alert`. Time-limited — always has an `expires` timestamp. Suppressions filter alerts out of the briefing. Critical alerts can break through warning-level suppressions via escalation override.

**`data` fields:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `metric` | string | No | Specific metric to suppress (e.g., `"cpu_pct"`). `null` means match by tags/source. |
| `suppress_level` | string | Yes | Maximum alert level to suppress: `"warning"` or `"critical"`. Default: `"warning"`. |
| `escalation_override` | boolean | Yes | If `true`, critical alerts break through even when the suppression matches. Default: `true`. |
| `reason` | string | No | Why the suppression was created (e.g., `"Known maintenance window"`). Default: `""`. |
| `tags` | array | No | Tags to match against alerts. Suppression matching checks for word overlap between these tags and alert fields. |

### `context` — Time-limited knowledge

Written by agents via `add_context`. Always has an `expires` timestamp (default: 30 days). Use for events, temporary situations, or facts that lose relevance over time.

**`data` fields:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `description` | string | Yes | Human-readable description (e.g., `"Kitchen renovation in progress — expect Home Assistant sensors to go offline"`). |

### `preference` — User preferences

Written by agents via `set_preference`. Keyed by `key` + `scope` (upserted). Portable across agent platforms — any agent reads the same preferences.

**`data` fields:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `key` | string | Yes | Preference name (e.g., `"alert_verbosity"`, `"check_frequency"`). |
| `value` | string | Yes | Preference value (e.g., `"one_sentence_warnings"`, `"first_turn_only"`). |
| `scope` | string | Yes | Scope of the preference. Default: `"global"`. Could be used for per-source or per-agent scoping. |

## Lifecycle

- **Upsert behavior:** `status` entries are upserted by `source`. `alert` entries by `source` + `alert_id`. `preference` entries by `key` + `scope`. Other types always insert new rows.
- **Soft delete:** `delete_entry` sets the `deleted` timestamp. Entry remains in the database for 30 days, recoverable via `restore_entry`. Bulk deletes require `confirm=True` (dry-run by default).
- **Auto-purge:** Expired entries (`expires < now`) and old soft-deleted entries (`deleted` > 30 days ago) are cleaned up by `_cleanup_expired`, which runs on a background thread triggered by write operations, debounced to at most every 10 seconds. Cleanup never blocks the request that triggers it — the debounce check is instant, and the actual DELETE runs on a separate thread with its own SQLite connection. Read operations do not trigger cleanup. If the server receives no write traffic, expired entries remain in the database until the next write. **Note:** auto-purge performs a hard `DELETE`, not a soft delete. Expired entries bypass the trash entirely — once past their expiry, they are permanently removed on the next cleanup pass.
- **Staleness:** Status entries with `ttl_sec` are marked stale in the briefing if no update arrives within the TTL window. The entry itself is not deleted — it remains as the last known state.
- **Hard deletes:** The API only performs soft deletes. If you delete the SQLite database file or run manual SQL `DELETE` statements, that data is gone permanently — there is no recovery mechanism beyond your own backups. Back up `awareness.db` regularly.

## SQLite configuration

- **WAL mode** enabled for concurrent read/write safety
- **Thread safety:** Write operations are protected by `threading.Lock` for async compatibility
- **Background cleanup:** `_cleanup_expired` spawns a daemon thread with its own SQLite connection, debounced to at most every 10 seconds, triggered only by writes
