# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **JWT auth middleware**: opt-in via `AWARENESS_AUTH_REQUIRED=true`, validates Bearer tokens, extracts owner_id from `sub` claim
- **Row-level security**: Postgres RLS policies on all data tables as defense-in-depth alongside application-level owner_id filtering
- **CLI: `mcp-awareness-user`**: add/list/set-password/export/delete users with email normalization, E.164 phone validation, argon2id password hashing
- **CLI: `mcp-awareness-token`**: generate JWTs for self-hosted multi-user deployments
- **CLI: `mcp-awareness-secret`**: generate 256-bit JWT signing secrets
- **New dependencies**: `PyJWT` (JWT validation), `argon2-cffi` (password hashing), `phonenumbers` (E.164 validation)
- **Multi-tenant schema**: `owner_id` column on all data tables (entries, reads, actions, embeddings) with backfill migration for existing data
- **Users table**: full user schema with email (+ canonical normalization for uniqueness), E.164 phone, argon2id password hash, timezone, preferences JSONB
- **Owner isolation**: all store methods, tools, resources, and collator now thread `owner_id` — queries are scoped per-owner
- **`AWARENESS_DEFAULT_OWNER`**: env var (falls back to system username) sets the default owner for stdio and unauthenticated HTTP

### Changed
- **`entries.updated` nullable**: column is now NULL on insert, set only on actual updates — aligns with `users.updated` semantics; sort and filter queries use `COALESCE(updated, created)` for consistency

### Security
- **`cleanup_expired` RLS-safe**: background cleanup now uses `SET LOCAL row_security = off` so expired entries are cleaned regardless of RLS enforcement
- **`clear()` scoped to owner**: `clear(owner_id)` deletes only that owner's data instead of truncating all tenants
- **Argon2 time_cost bumped to 3**: stronger password hashing for new and changed passwords (existing hashes remain valid)

### Fixed
- **PR label automation**: `Dev Active` is now a proper hold state — `on-push` and `on-ci-pass` skip pipeline transitions while it's present, `on-unlabel` handles promotion when it's removed
- **PR label automation**: `on-ci-pass` no longer fails on force-pushed PRs — `gh api` 404 errors handled gracefully
- **PR label automation**: removing `Dev Active` checks CI status (via workflow runs API, job-name-agnostic) and promotes to `Ready for QA` or `Awaiting CI` accordingly
- **PR label automation**: adding `Dev Active` now also clears `Awaiting CI` and `Ready for QA` to prevent competing state
- **PR label automation**: added explicit `checks: read` permission
- **PR label automation**: `on-ci-pass` now finds PRs from dependabot and other non-default branches by falling back to head branch search when the `pull_requests` array is empty

## [0.14.0] - 2026-03-28

### Changed
- **License changed from Apache 2.0 to AGPL v3** to protect against proprietary cloud hosting of the codebase. Dual-licensing path preserved for future commercial license.

### Added
- `NOTICE` file documenting the license change, prior license, and rationale
- `CONTRIBUTING.md` with Contributor License Agreement (CLA) requirement
- `benchmarks/semantic_search_bench.py` — latency benchmarks for semantic search across scale tiers (500–10K entries)
- **PR label automation** (`pr-labels.yml`): GitHub Actions workflow that automates label transitions — resets to "Awaiting CI" on push, promotes to "Ready for QA" when CI passes, cleans up stale labels when actors pick up tasks
- **Favicon route**: `/favicon.ico` served from both `SecretPathMiddleware` and `HealthMiddleware` so Anthropic's Connectors UI (and other services using Google's favicon service) display the awareness logo instead of a generic globe. Served publicly — no secret path required.

## [0.12.0] - 2026-03-26

### Added
- **`__main__.py` entry point**: `python -m mcp_awareness` now works correctly
- **Coverage tests for prompt and restore branches**: 10 tests covering agent_instructions fallback, project_context alerts/truncation, system_status description/alerts/patterns, write_guide tag overflow, catchup alerts/truncation, restore_entry by tags and no-args
- **Tests for SecretPathMiddleware and HealthMiddleware ASGI classes**: extracted middleware to `middleware.py` and added 10 tests covering path rewriting, health endpoints, 404 responses, and scope passthrough
- Concurrency tests for connection pool, background cleanup, and concurrent upserts
- **Embedding round-trip tests**: compose → store → search pipeline, stale detection, filtered search
- **Store protocol docstrings**: Concise one-line docstrings for all ~30 methods in the `Store` protocol, documenting the contract for backend implementors
- `uv.lock` for reproducible dependency resolution across builds
- **Branding assets**: 9 SVG logo variants (icon sizes 16–200px, light/dark, wordmark light/dark) and favicon.ico in `docs/branding/`
- **README logo header**: Wordmark hero replaces plain `# mcp-awareness` heading, centered badge row
- **Integration tests for server startup**: health endpoint, secret path middleware routing, and MCP endpoint — covers `_run()`, `_create_store()`, middleware instantiation, and transport config

### Changed
- **Split `server.py` into focused modules**: Extracted tool handlers (`tools.py`), resource handlers (`resources.py`), prompt handlers (`prompts.py`), and shared helpers (`helpers.py`) from the 1,718-line `server.py` for maintainability
- Tag filtering in `get_entries` and `get_knowledge` now uses AND logic (match ALL tags) instead of OR, consistent with delete/restore operations
- **README**: Remove stale "proof of concept" framing — project is production-deployed
- Dockerfile uses `uv` for deterministic installs

### Fixed
- Remove dead `if __name__ == "__main__"` block from `server.py` (caused circular import)
- Embedding vector dimension is now configurable via `AWARENESS_EMBEDDING_DIMENSIONS` in both the provider and the DDL (was hardcoded to 768 in the schema)
- Background embedding now uses the connection pool via `store.upsert_embedding()` instead of duplicated SQL with a dedicated connection
- Replace silent `except Exception: pass` blocks with `logger.debug` logging in server and store
- `upsert_by_logical_key` race condition: concurrent writers can no longer create duplicate entries
- Logical key unique index now excludes soft-deleted entries, allowing re-creation after delete
- Invalid `entry_type` parameter now returns structured error instead of unhandled ValueError
- `get_related` now fetches forward references in a single query instead of N individual lookups
- Restoring soft-deleted entries now recovers original expiry instead of setting it to NULL
- Catchup prompt now pushes `since` filter to SQL instead of loading all entries into Python
- All client-facing query tools now apply a default LIMIT (200) to prevent unbounded result sets
- Added `limit` parameter to `get_unread` tool
- `backfill_embeddings` now batches embedding generation instead of making individual API calls per entry

## [0.11.2] - 2026-03-25

### Added
- **Query discipline in server instructions**: MCP instructions now guide clients to use `mode='list'` before full fetches, set `limit`, use `hint` for relevance ranking, narrow with specific tags, and check `get_stats`/`get_tags` before broad queries
- **Externalized server instructions**: MCP instructions moved from inline Python string to `instructions.md` for cleaner content/code separation
- **Case studies doc**: `docs/case-studies.md` with 7 real-world examples of awareness in practice, each attributed to the agent/platform involved

### Changed
- **README aspirational claims**: Replaced doctor appointment scenario with grounded cross-platform example, qualified "family schedules, health data" as planned edge capabilities, reframed intentions section around working features (time-based firing) with location-based noted as planned
- **README "How it's built" section**: Condensed inline examples to a summary paragraph with link to case studies doc
- **Human-directed framing**: All documentation now explicitly credits the user directing the work, not agents acting autonomously
- **Copyright footers**: Added to all 5 docs that were missing them
- **Docker Compose**: Use `pull_policy: always` for production deployment

## [0.11.1] - 2026-03-24

### Fixed
- **`parse_iso` naive datetime**: Inputs without timezone suffix now default to UTC instead of returning naive datetimes that cause `TypeError` on comparison
- **`count_active_suppressions` missing expiry filter**: Now excludes expired suppressions, consistent with `get_active_suppressions`
- **`get_knowledge` filter bypass**: When `entry_type` was set, `until`, `learned_from`, `created_after`, `created_before`, and `include_history` were silently dropped. All filters now apply regardless of `entry_type`
- **`delete_entry` dry-run/confirm count mismatch**: Dry-run used OR tag logic but actual delete used AND. Both now use AND semantics
- **`compose_embedding_text` missing entry type**: Note and context entries with identical source/tags/description now produce different embeddings

### Added
- **Planned edge providers** section in README: documents five provider categories (Calendar, GPS, NAS, Health, Vision) with multi-edge correlation design
- Input validation for enum-like parameters: `level` (warning/critical), `alert_type` (threshold/structural/baseline), `urgency` (low/normal/high)
- Bounds validation for `limit`, `offset`, `expires_days`, and `duration_minutes` — negative values return clear error messages instead of Postgres exceptions
- 15 new tests (333 total)

### Changed
- **Tool description heuristics**: Rewritten docstrings for `remember`, `add_context`, `learn_pattern`, and `remind` with decision heuristics that help agents choose the right tool. Each includes a "quick test" rule: still true in 30 days? → `remember`. Happening now, will become stale? → `add_context`. Has a "when X, expect Y" rule? → `learn_pattern`. `remind` language softened from formal "intentions" to friendlier "todos, reminders, and planned actions."
- **Connection pooling**: `PostgresStore` now uses `psycopg_pool.ConnectionPool` (min 2, max 5 connections) instead of a single shared connection. Concurrent HTTP requests no longer serialize. Background threads (embedding, cleanup) draw from the pool instead of needing dedicated connections. The hand-rolled `_conn` health check property is removed — the pool handles reconnection, health checks, and connection recycling automatically.
- **docker-compose.yaml**: Image tag changed from pinned version to `:latest`. No more manual version bumps on release.
- **docker-compose.yaml**: Added `pull_policy: always` so `docker compose up -d` always pulls the latest image. Removed `build: .` — production deployments should never build from source. Exposed port 8420 on localhost for direct access.

## [0.11.0] - 2026-03-24

### Added
- **Background embedding generation**: Write tools now submit embedding generation to a thread pool (max 2 workers) instead of blocking the response. ~100-200ms latency removed from writes.
- **`backfill_embeddings` tool**: Embeds entries created before the provider was configured, and re-embeds entries whose content changed since their last embedding (stale detection via `text_hash`).
- **`hint` parameter on `get_knowledge`**: Re-ranks tag-filtered results by semantic similarity to a natural language phrase. Example: `get_knowledge(tags=["finance"], hint="retirement savings")`. Results include `similarity` scores when hint is active.
- **Stale embedding detection**: `get_stale_embeddings` store method finds entries whose text changed after their embedding was generated.
- **`get_related` tool**: Bidirectional entry relationship traversal. Returns entries referenced via `related_ids` in data, plus entries that reference the given entry. Convention: store `related_ids: [...]` in entry data when using `remember` or `learn_pattern`.
- 76 new tests (315 total)

### Fixed
- **JSON content field**: `remember` and `update_entry` now accept JSON objects/arrays in the `content` field. Pydantic deserializes JSON strings into dicts before the str validator runs — content is now re-serialized to string when this happens.
- **Connection resilience**: `PostgresStore` now auto-heals dead database connections. A health check runs every 30 seconds via a `_conn` property — if the connection is closed or broken, it reconnects transparently. No more permanent dead connections after Postgres restarts.

## [0.10.0] - 2026-03-23

### Added
- **Semantic search (RAG)**: New `semantic_search` tool finds entries by meaning using vector similarity. Powered by pgvector + Ollama (optional, self-hosted).
- **Embedding provider abstraction**: `EmbeddingProvider` protocol with `OllamaEmbedding` and `NullEmbedding` implementations. Swappable backends.
- **Embedding on write**: Write tools (`remember`, `learn_pattern`, `add_context`, `report_alert`, `report_status`, `update_entry`) auto-generate embeddings when a provider is configured. Currently synchronous; background generation planned for Phase 2.
- **Embeddings table**: Separate table with HNSW vector index, `ON DELETE CASCADE` from entries, unique constraint per entry+model.
- **Docker Compose Ollama service**: Optional `ollama` service under `embeddings` profile for local embedding generation.
- **Configuration**: `AWARENESS_EMBEDDING_PROVIDER`, `AWARENESS_EMBEDDING_MODEL`, `AWARENESS_OLLAMA_URL` env vars (all optional — system works without them).
- **`created_after`/`created_before` filters**: `get_knowledge(created_after="...", created_before="...")` filters by creation time, distinct from `since`/`until` which filter by last update time.
- **Ollama in CI**: GitHub Actions service container with `nomic-embed-text` model for integration testing.
- 55 new tests (294 total)

### Fixed
- **Internal API coupling**: Documented and isolated `_prompt_manager._prompts` access in custom prompt sync — no public remove API exists in FastMCP, so private access is contained to deletion only.

## [0.9.0] - 2026-03-23

### Changed
- **SQL-level pagination**: LIMIT/OFFSET pushed from Python to SQL in `_query_entries`. All list methods (`get_entries`, `get_knowledge`, `get_active_alerts`, `get_deleted`, `get_intentions`) now paginate at the database level.
- **Default sort order**: All queries return most recently updated entries first (`ORDER BY updated DESC`). Previously returned oldest first.
- **Resolved alert filter in SQL**: `get_active_alerts` now filters resolved alerts via `NOT (data @> '{"resolved": true}'::jsonb)` instead of post-fetch Python filtering.
- **Suppression expiry filter in SQL**: `get_active_suppressions` now filters expired suppressions via `expires IS NULL OR expires > NOW()` instead of relying on the collator.
- **`to_list_dict` type-aware**: List mode now uses `message` as description for alerts and includes `goal`/`state` for intentions.

### Added
- **`until` parameter**: `get_knowledge(until="...")` filters by `updated <= timestamp`. Combine with `since` for date ranges.
- **`learned_from` parameter**: `get_knowledge(learned_from="claude-code")` filters by the platform that created the entry.
- 7 new tests (238 total)

### Fixed
- `examples/test_new_tools.py` referenced stale `AWARENESS_DATA_DIR` env var

## [0.8.0] - 2026-03-23

### Added
- **INTENTION entry type**: Goals with constraints, evaluated when conditions align. New lifecycle: pending → fired → completed/snoozed/cancelled.
- **`remind` tool**: Create intentions with optional `deliver_at` timestamp, constraints, urgency. Time-based triggers fire automatically in the briefing.
- **`get_intentions` tool**: Query intentions by state, source, tags. Supports list mode.
- **`update_intention` tool**: Transition intention state (fire, complete, snooze, cancel) with optional reason. Changelog tracked.
- **Briefing integration**: Collator evaluates pending intentions — surfaces `fired_intentions` when `deliver_at` has passed. Summary includes intention count. Evaluation field tracks `intentions_pending` and `intentions_fired`.
- 17 new tests (230 total)

## [0.7.0] - 2026-03-23

### Added
- **Read tracking**: Auto-logs when entries are accessed by `get_knowledge` and `get_alerts`. Query with `get_reads(entry_id?, since?, platform?, limit?)`.
- **Action tracking**: `acted_on(entry_id, action, platform?, detail?, tags?)` records concrete actions agents take because of entries. Query with `get_actions(entry_id?, since?, platform?, tags?, limit?)`.
- **Unread entries**: `get_unread(since?)` returns entries with zero reads — cleanup candidates and dead knowledge.
- **Activity feed**: `get_activity(since?, platform?, limit?)` returns combined reads + actions chronologically.
- **Read count enrichment**: List mode (`mode="list"`) now includes `read_count` and `last_read` on each entry.
- **Actions have tags**: Tags on action records (default: copied from referenced entry) enable filtered action queries.
- Alembic migration for `reads` and `actions` tables with indexes
- 17 new tests (213 total)

## [0.6.1] - 2026-03-23

### Added
- **Evaluation transparency**: Briefing includes an `evaluation` field showing what the collator checked and dismissed: `{alerts_checked, suppressed, pattern_matched, stale_sources, surfaced}`. Makes silence tangible — confirms nothing was missed, not that nothing was checked.
- **Vision document**: [`docs/vision.md`](docs/vision.md) — what knowledge becomes when it's ambient: silence, estate planning, place memory, relationship mirror, decision archaeology, community memory, and the INTENTION concept
- README Vision section rewritten with link to full document
- Historical-design notes on spec docs (from-metrics-to-mental-models.md, collation-layer.md)
- 6 new tests (196 total)

## [0.6.0] - 2026-03-23

### Added
- **List mode**: `get_knowledge(mode="list")` returns metadata only (id, type, source, description, tags, created, updated) — no content or changelog. Also available on `get_alerts` and `get_deleted`. Use to orient before pulling full entries.
- **Since filter**: `get_knowledge(since="2026-03-23T06:00:00Z")` returns only entries updated after the given timestamp. SQL-level filtering (not post-query). Also available on `get_alerts`, `get_entries`, and `get_deleted`.
- **Codecov coverage**: CI uploads coverage reports; badge on README
- **README badges**: CI, coverage, Python versions, license, Docker image
- Testcontainers for Postgres-based test suite (190 tests)

### Changed
- **`get_knowledge` source filter at SQL level**: `source` parameter now pushed to PostgresStore SQL query instead of Python-side post-filtering
- **Empty `since` validation**: `get_knowledge`, `get_alerts`, and `get_deleted` now return an error for empty-string `since` instead of silently ignoring it
- **PostgreSQL is the only backend** — SQLiteStore removed (~560 lines). All tests run against real Postgres via testcontainers. The Store protocol remains as the backend interface for future implementations.
- **Lazy store initialization**: Server module no longer creates a DB connection at import time — store initializes on first access. Fixes review issue #7 (module-level side effects).
- **psycopg and alembic are core dependencies** (moved from optional `[postgres]` extra)
- Dockerfile installs base package (no `[postgres]` extra needed)
- `AWARENESS_BACKEND` and `AWARENESS_DATA_DIR` env vars removed (Postgres-only)
- `docker-entrypoint.sh` runs migrations unconditionally
- `docker-compose.yaml` updated: uses published GHCR image, Postgres default, ports commented out, project name set, hardcoded tunnel credential UUID removed

### Fixed
- **Cleanup thread accumulation**: Background cleanup now checks if previous thread is still alive before spawning a new one
- **pyproject.toml version**: Bumped from 0.1.0 to 0.6.0 to match release

### Removed
- SQLiteStore backend (`store.py` reduced to Store protocol only)
- `examples/migrate_sqlite_to_postgres.py` (no SQLite to migrate from)
- `AwarenessStore` backward-compatibility alias

## [0.5.0] - 2026-03-23

### Added
- **MCP Prompts** — 5 dynamic prompts built from store data:
  - `agent_instructions` — complete workflow conventions from `source="awareness-prompt"` entries
  - `project_context(repo_name)` — knowledge, alerts, and status for a project
  - `system_status(source)` — status, alerts, and patterns for a monitored system
  - `write_guide` — existing sources, tags with counts, and entry type reference
  - `catchup(hours)` — what changed recently across knowledge and alerts
- **User-defined prompts**: store entries with `source="custom-prompt"` and they automatically appear as MCP prompts. Template variables (`{{var}}`) become prompt arguments. Prompts are namespaced under `user/` and synced dynamically on every list/get.
- **Delete by tags**: `delete_entry(tags=["qa-test"], confirm=True)` soft-deletes all entries matching ALL given tags (AND logic). Supports dry-run without `confirm`.
- **Restore by tags**: `restore_entry(tags=["qa-test"])` restores all trashed entries matching ALL given tags (AND logic).
- 19 new tests (181 total)
- **One-line demo install**: `install-demo.sh` script downloads a Docker Compose file, starts Awareness + Postgres + Cloudflare quick tunnel, and prints ready-to-paste MCP config snippets for all clients
- **Published Docker image**: `ghcr.io/cmeans/mcp-awareness` built and pushed to GHCR automatically on version tags via GitHub Actions
- **Demo seed data**: fresh instances are pre-loaded with getting-started knowledge, example prompts, and an onboarding prompt that interviews users to personalize their instance
- **Dockerfile hardened**: runs as non-root `awareness` user, OCI image labels (title, source, license, author), no anonymous volumes

## [0.4.1] - 2026-03-22

### Fixed
- **Cleanup error logging**: `_do_cleanup` now logs exceptions instead of silently swallowing them (both SQLite and Postgres backends)
- **O(n) upsert_alert**: Alert lookup pushed into SQL (`json_extract`/`data->>'alert_id'`) instead of loading all alerts and scanning in Python. Same fix applied to `upsert_preference`

### Added
- **Pagination**: `limit`/`offset` params on `get_knowledge`, `get_alerts`, `get_entries`, `get_deleted` tools and Store protocol methods
- **QA gate**: `QA Approved` label required to merge PRs, enforced by `qa-gate.yml` workflow (pending status, not failed)
- QA section conventions in CLAUDE.md: MCP-based manual tests with copyable code blocks
- 7 new tests (162 total)

## [0.4.0] - 2026-03-22

### Added
- **Alembic migrations**: version-tracked database migrations for PostgreSQL (raw SQL, no ORM)
- **`mcp-awareness-migrate` CLI**: run/stamp/check/history for database migrations
- **pgvector extension**: enabled via migration (ready for future RAG/embeddings)
- **Migration files**: initial schema baseline + pgvector extension

### Changed
- Removed inline migration code from PostgresStore (Alembic handles it)
- Dockerfile includes alembic.ini and migration files

## [0.3.1] - 2026-03-21

### Added
- **`logical_key` upsert**: Optional `logical_key` param on `remember` enables idempotent upserts — same source + logical_key updates in place with changelog tracking, no UUID needed
- **Partial unique index** on `(source, logical_key)` for both SQLite and PostgreSQL
- **Postgres migration**: auto-adds `logical_key` column to existing databases on startup
- **Cross-platform feedback loop story** in README "How it's built" section
- 7 new tests (155 total)

### Changed
- Production deployment switched from SQLite to PostgreSQL backend
- Dockerfile installs `[postgres]` extra (psycopg)
- Docker Compose: Postgres service in default profile, mcp-awareness depends on postgres health
- Clean shutdown on Ctrl+C — `KeyboardInterrupt` prints "Shutdown requested" instead of traceback
- SQLite `CREATE TABLE` includes `logical_key` column (migration still handles existing DBs)

### Fixed
- Postgres migration: `logical_key` index creation moved after column migration
- SQLite migration: same fix — index creation after column addition
- MCP session manager initialization for non-MOUNT_PATH HTTP transport

## [0.3.0] - 2026-03-21

### Added
- **`note` entry type**: General-purpose permanent knowledge with optional `content` payload and MIME `content_type`
- **`remember` tool**: Create notes — personal facts, project notes, skill backups, config snapshots
- **`update_entry` tool**: Update knowledge entries (note/pattern/context/preference) in place with `changelog` tracking. Status/alert/suppression are immutable
- **`get_stats` tool**: Entry counts by type, list of sources, total count
- **`get_tags` tool**: All tags with usage counts — prevents tag drift across platforms
- **`/health` endpoint**: Pure HTTP health check (no MCP overhead) returning uptime, timestamp, transport
- **Request timing**: `@_timed` decorator on all 18 tools and 6 resources logs wall-clock time per call to stdout
- **PostgreSQL backend**: `PostgresStore` with JSONB, GIN indexes, pgvector-ready. Opt-in via `AWARENESS_BACKEND=postgres`
- **Docker Compose Postgres service**: `pgvector/pgvector:pg17` with `wal_level=logical` for Debezium CDC readiness
- **Migration script**: `examples/migrate_sqlite_to_postgres.py` for SQLite → Postgres data migration
- **`include_history` param** on `get_knowledge`: omit (strip changelog), `"true"` (include), `"only"` (only entries with changes)
- **Memory prompts documentation** (`docs/memory-prompts.md`): three tiers of prompt integration (platform memory, global CLAUDE.md, project CLAUDE.md) with tuning cycle guidance
- **Awareness workflow in project CLAUDE.md**: verify connection, check context, maintain status, record milestones
- **Vision section in README**: personal → family → team → organization progression, universal context, bidirectional data flow, proactive intelligence
- CI status checks required on branch protection (lint, test 3.10/3.11/3.12, typecheck)
- 148 tests (up from 124), strict type checking

### Changed
- `_cleanup_expired` now runs on a background daemon thread — never blocks the calling request
- Cleanup removed from read paths; only writes trigger it
- `get_knowledge` tool now accepts `source`, `tags`, `entry_type`, and `include_history` params for filtered queries
- Suppression tags stored only in entry envelope (removed duplicate `data.tags` field)
- Tool descriptions include structured error contract (unstructured errors = transport/platform failure)
- Renamed `_changelog` to `changelog` (no underscore — it's public API, not internal)
- Comprehensive README rewrite: simpler language, "shared memory for every AI you use", cross-platform workflow story
- Quick Start simplified to `docker compose up -d` (moved pip install to Development section)
- Docker health check: `start_period` 5s→15s, `timeout` 5s→10s to prevent false failures on startup
- Deployment guide updated with Docker quick start, current tool surface, memory prompt link
- Entry timestamps migrated from `str` to `datetime` objects — PostgreSQL uses `TIMESTAMPTZ`, SQLite converts at boundary
- PostgresStore uses GIN-indexed `@>` operator for tag filtering (pushed from Python to SQL)

### Fixed
- Data model inconsistency: suppression tag matching now uses envelope `tags` instead of `data.tags`

## [0.2.0] - 2026-03-20

### Added
- **Storage abstraction**: `Store` protocol with `SQLiteStore` default implementation — future backends swap without changing collator or server
- **Soft delete**: `delete_entry` moves to trash (30-day retention), `restore_entry` recovers, `get_deleted` lists trash
- **Bulk delete safety**: dry-run by default, requires `confirm=True`
- **Secure deployment**: secret path auth via `AWARENESS_MOUNT_PATH` env var with server middleware
- **Docker Compose**: named Cloudflare Tunnel and ephemeral quick tunnel profiles
- **Streamable HTTP transport**: `AWARENESS_TRANSPORT=streamable-http` for remote clients
- **Read tools**: mirrors of all resources for tools-only MCP clients (Claude.ai)
- **Knowledge tools**: `learn_pattern`, `add_context`, `set_preference`, `get_knowledge`
- **Suppression tag matching**: checks alert content fields (alert_id, message), not just structural tags
- **Data dictionary**: full schema documentation for entries table and all 6 entry types
- **Deployment guide**: walkthrough with Cloudflare Tunnel, WAF, and Claude.ai connector setup
- **Issue templates**: bug report, feature request, platform test report
- **GitHub Sponsors**: FUNDING.yml with 4 monthly tiers
- **CI pipeline**: ruff, mypy (strict), pytest via GitHub Actions on push/PR to main
- 124 tests with strict type checking

### Changed
- README reframed from monitoring to portable knowledge store
- Demo data reworked to show knowledge store capabilities (not just NAS monitoring)
- `poc-demo.md` renamed to `deployment-guide.md`
- License changed from MIT to Apache 2.0

## [0.1.0] - 2026-03-18

Initial implementation.

### Added
- **Core server**: FastMCP server with stdio transport
- **Entry types**: status, alert, pattern, suppression, context, preference
- **Collator**: briefing generation with suppression and pattern application
- **Briefing resource**: `awareness://briefing` — compact, token-optimized summary
- **Drill-down resources**: alerts, status, knowledge, suppressions
- **Three-layer detection model**: threshold and knowledge layers (baseline planned)
- **Pattern matching**: word-overlap between effect strings and alert fields
- **Suppression system**: time-based expiry with escalation overrides (critical breaks through)
- **SQLite backend**: WAL mode, threading.Lock for async safety
- **Dockerfile** for container deployment
- Design docs: core spec and collation layer

[Unreleased]: https://github.com/cmeans/mcp-awareness/compare/v0.14.0...HEAD
[0.14.0]: https://github.com/cmeans/mcp-awareness/compare/v0.12.0...v0.14.0
[0.12.0]: https://github.com/cmeans/mcp-awareness/compare/v0.11.2...v0.12.0
[0.11.2]: https://github.com/cmeans/mcp-awareness/compare/v0.11.1...v0.11.2
[0.11.1]: https://github.com/cmeans/mcp-awareness/compare/v0.11.0...v0.11.1
[0.11.0]: https://github.com/cmeans/mcp-awareness/compare/v0.10.0...v0.11.0
[0.10.0]: https://github.com/cmeans/mcp-awareness/compare/v0.9.0...v0.10.0
[0.9.0]: https://github.com/cmeans/mcp-awareness/compare/v0.8.0...v0.9.0
[0.8.0]: https://github.com/cmeans/mcp-awareness/compare/v0.7.0...v0.8.0
[0.7.0]: https://github.com/cmeans/mcp-awareness/compare/v0.6.1...v0.7.0
[0.6.1]: https://github.com/cmeans/mcp-awareness/compare/v0.6.0...v0.6.1
[0.6.0]: https://github.com/cmeans/mcp-awareness/compare/v0.5.0...v0.6.0
[0.5.0]: https://github.com/cmeans/mcp-awareness/compare/v0.4.1...v0.5.0
[0.4.1]: https://github.com/cmeans/mcp-awareness/compare/v0.4.0...v0.4.1
[0.4.0]: https://github.com/cmeans/mcp-awareness/compare/v0.3.1...v0.4.0
[0.3.1]: https://github.com/cmeans/mcp-awareness/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/cmeans/mcp-awareness/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/cmeans/mcp-awareness/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/cmeans/mcp-awareness/releases/tag/v0.1.0
