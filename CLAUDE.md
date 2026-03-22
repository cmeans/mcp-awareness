# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repository Purpose

**mcp-awareness** — a generic MCP server that provides ambient system awareness to AI agents. Edge processes (NAS daemons, calendar processors, CI/CD watchers) write tagged status, alerts, and knowledge. Any MCP client reads a unified, token-optimized briefing.

## PR conventions

Every PR that changes functionality must include:
- **CHANGELOG update** — add entries under `[Unreleased]` following Keep a Changelog format
- **README update** — if the change affects "Current status", "Implemented", test count, or tool count, update those sections
- **Test count** — update the test count in README if tests were added or removed
- **QA section** — every PR body must include a `## QA` section with:
  - **Prerequisites**: environment setup needed (e.g., install deps, deploy to test instance). Use an alternate port (e.g., `AWARENESS_PORT=8421`) to avoid breaking production
  - **Manual tests**: all testing must go through the MCP interface (call awareness tools directly, not raw HTTP/curl). Each test step should:
    - Have a checkbox (`- [ ]`) to mark pass/fail
    - Name the MCP tool to call and the arguments to pass
    - Describe the expected outcome explicitly
  - **Automated checks**: checkboxes for `pytest`, `ruff`, `mypy` results
  - Example format:
    ```markdown
    ## QA

    ### Prerequisites
    - `pip install -e ".[dev]"`
    - Deploy to test instance on alternate port (`AWARENESS_PORT=8421`)

    ### Automated checks
    - [ ] `python -m pytest tests/` — all pass
    - [ ] `ruff check src/ tests/` — clean
    - [ ] `mypy src/mcp_awareness/` — clean

    ### Manual tests (via MCP tools)
    1. - [ ] **Description of test**
       ```
       tool_name(arg1="value", arg2="value")
       ```
       Expected: description of what success looks like
    ```
  - **Note**: CI already runs pytest, ruff, and mypy — only include automated checks in QA if you will auto-verify and check them off. Otherwise, omit them and let CI enforce.
- **Data dictionary** — update `docs/data-dictionary.md` if schema changed

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
├── schema.py      # Entry types (status/alert/pattern/suppression/context/preference/note),
│                  # common envelope, validation, TTL/expiry logic, severity ranking
├── store.py           # Store protocol + SQLiteStore implementation (WAL mode), CRUD, soft delete, TTL cleanup
├── postgres_store.py  # PostgresStore implementation (psycopg, JSONB, GIN indexes, pgvector-ready)
├── collator.py        # Briefing generation: applies suppressions + patterns, composes summary/mention
└── server.py          # FastMCP server wiring — resources (read) + tools (write/update) + secret path middleware
```

**Data flow**: Edge processes → tools (`report_status`, `report_alert`) → `store` → `collator.generate_briefing()` → `awareness://briefing` resource

**Storage abstraction**: `Store` protocol defines the interface; `SQLiteStore` (default) and `PostgresStore` (opt-in) implement it. Backend selected via `AWARENESS_BACKEND` env var. The collator depends on the protocol, not the concrete class. PostgresStore uses JSONB with GIN indexes for tags, psycopg sync driver, and pgvector extension (installed, ready for RAG). `wal_level=logical` configured for Debezium CDC readiness.

**Key design decisions**:
- Briefing is computed on-demand per read (not background task) — fine for SQLite with WAL
- Seven entry types: status, alert, pattern, suppression, context, preference, note
- One status entry per source (upsert), alerts keyed by source + alert_id, preferences upsert by key + scope
- Notes support optional content payload with MIME content_type
- update_entry works on knowledge types only (note/pattern/context/preference); status/alert/suppression are immutable. Changes tracked in changelog array
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

## Working with awareness

If you have access to the awareness MCP server while working on this repo:
- **Verify connection:** Call `get_briefing` at the start of work. If it fails or returns an unstructured error, awareness is not reachable — skip the remaining steps.
- **Check context:** Call `get_knowledge(tags=["mcp-awareness"])` to see if other agents or platforms left relevant context.
- **Maintain status:** Keep a single permanent status note for this project using `remember`, then update it with `update_entry` as work progresses. Use tags `["mcp-awareness", "project", "status"]`. The `changelog` tracks history automatically.
- **Record milestones:** When finishing significant work (PR merged, release tagged), update the status note so other platforms know what happened.
- **Code ships before prompts.** Never update awareness prompt entries for features that aren't deployed yet. Agents will try to use tools that don't exist on the running server. Merge PR → rebuild Docker → verify deployment → then update prompts.

## Naming

- Package: `mcp-awareness-server` (PyPI)
- Import: `mcp_awareness`
- FastMCP name: `mcp-awareness`
- CLI entry point: `mcp-awareness`
- Repo: `cmeans/mcp-awareness`
- Edge daemons are separate repos (e.g., `homelab-edge`) — this repo is only the generic awareness service
