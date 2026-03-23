# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

[Unreleased]: https://github.com/cmeans/mcp-awareness/compare/v0.5.0...HEAD
[0.5.0]: https://github.com/cmeans/mcp-awareness/compare/v0.4.1...v0.5.0
[0.4.1]: https://github.com/cmeans/mcp-awareness/compare/v0.4.0...v0.4.1
[0.4.0]: https://github.com/cmeans/mcp-awareness/compare/v0.3.1...v0.4.0
[0.3.1]: https://github.com/cmeans/mcp-awareness/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/cmeans/mcp-awareness/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/cmeans/mcp-awareness/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/cmeans/mcp-awareness/releases/tag/v0.1.0
