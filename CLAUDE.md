# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repository Purpose

**mcp-awareness** — a generic MCP server that provides ambient system awareness to AI agents. Edge processes (NAS daemons, calendar processors, CI/CD watchers) write tagged status, alerts, and knowledge. Any MCP client reads a unified, token-optimized briefing.

## Build & Test

```bash
pip install -e ".[dev]"   # install with dev dependencies (ruff, mypy, pytest, pytest-cov)
python -m pytest tests/   # run all tests
python -m pytest tests/test_collator.py::TestIsSuppressed::test_escalation_override_breaks_through  # single test
ruff check src/ tests/    # lint
ruff format src/ tests/   # format (or --check to verify)
mypy src/mcp_awareness/   # type check (strict mode)
mcp-awareness             # run server via stdio
AWARENESS_DATA_DIR=./data mcp-awareness  # custom data dir
```

CI runs all three (ruff, mypy, pytest) on push/PR to main via `.github/workflows/ci.yml`.

## Architecture

```
src/mcp_awareness/
├── schema.py      # Entry types (status/alert/pattern/suppression/context/preference),
│                  # common envelope, validation, TTL/expiry logic, severity ranking
├── store.py       # Store protocol + SQLiteStore implementation (WAL mode), CRUD, soft delete, TTL cleanup
├── collator.py    # Briefing generation: applies suppressions + patterns, composes summary/mention
└── server.py      # FastMCP server wiring — resources (read) + tools (write) + secret path middleware
```

**Data flow**: Edge processes → tools (`report_status`, `report_alert`) → `store` → `collator.generate_briefing()` → `awareness://briefing` resource

**Storage abstraction**: `Store` protocol defines the interface; `SQLiteStore` is the default implementation. `AwarenessStore` is a backward-compat alias. The collator depends on the protocol, not the concrete class — future backends (Postgres, hybrid) can be swapped without changing collator or server logic.

**Key design decisions**:
- Briefing is computed on-demand per read (not background task) — fine for SQLite with WAL
- One status entry per source (upsert), alerts keyed by source + alert_id, preferences upsert by key + scope
- Suppressions use expiry timestamps + escalation override (critical breaks through warning-level suppression)
- Pattern matching uses word-overlap between effect string and alert fields (hyphens/dashes normalized); hour ranges handle overnight wraparound
- Soft delete: `delete_entry` moves to trash (30-day retention), `restore_entry` recovers, `get_deleted` lists trash. Bulk deletes require `confirm=True` (dry-run by default). Auto-purged by existing `_cleanup_expired`.
- Resource descriptions carry behavioral hints — duplicate guidance in both server instructions and docstrings
- Store uses threading.Lock on writes for async safety; _cleanup_expired spawns a background daemon thread (never blocks the caller), debounced (10s interval), only triggered by writes
- Transport: stdio (default) or streamable-http via AWARENESS_TRANSPORT env var; HTTP on AWARENESS_HOST:AWARENESS_PORT/mcp
- Secret path auth: `AWARENESS_MOUNT_PATH` env var (e.g., `/my-secret`) rewrites `/my-secret/mcp` → `/mcp`, returns 404 for all other paths. Used with Cloudflare WAF to block unauthenticated traffic at the edge.

## Deployment

Docker Compose runs both the server and a Cloudflare named tunnel. See `docker-compose.yaml`.
- Named tunnel: `docker compose up -d` (requires `~/.cloudflared/` credentials)
- Quick tunnel: `docker compose --profile quick up -d mcp-awareness tunnel-quick` (ephemeral URL, no account needed)
- Data is bind-mounted from host (default `~/awareness`, configurable via `AWARENESS_DATA`)

## Key Documents

- `docs/from-metrics-to-mental-models.md` — Core spec: three-layer detection model, API design, data schema, priority table
- `docs/collation-layer.md` — Collation layer: briefing resource, token optimization, escalation logic, backend placement

## Naming

- Package: `mcp-awareness-server` (PyPI)
- Import: `mcp_awareness`
- FastMCP name: `mcp-awareness`
- CLI entry point: `mcp-awareness`
- Repo: `cmeans/mcp-awareness`
- Edge daemons are separate repos (e.g., `homelab-edge`) — this repo is only the generic awareness service
