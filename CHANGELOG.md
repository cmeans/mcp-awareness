<!-- SPDX-License-Identifier: AGPL-3.0-or-later | Copyright (C) 2026 Chris Means -->
# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **Unsupported-language alerts** — when lingua detects a language not in the regconfig mapping, write tools fire an info-level structural alert (`unsupported-language-{iso}`). One alert per unsupported language (upsert, not duplicate). Signals demand for Phase 3 non-Western language support. New `detect_language_iso` function in `language.py` returns raw ISO code even for unmapped languages. Refs [#264](https://github.com/cmeans/mcp-awareness/issues/264), [#238](https://github.com/cmeans/mcp-awareness/issues/238).
- **Language backfill migration** — Alembic data migration detects language on existing entries via lingua-py and updates the `language` column. Processes in batches, idempotent, gracefully skips if lingua is not installed. Refs [#263](https://github.com/cmeans/mcp-awareness/issues/263), [#238](https://github.com/cmeans/mcp-awareness/issues/238).
- **`get_knowledge` language filter** — optional `language` parameter (ISO 639-1) filters entries by their stored regconfig. Refs [#262](https://github.com/cmeans/mcp-awareness/issues/262), [#238](https://github.com/cmeans/mcp-awareness/issues/238).
- **`search` tool** — renamed from `semantic_search` to reflect the hybrid vector + FTS nature. `semantic_search` remains as a deprecated alias (delegates to `search`) and will be removed in a future release. Refs [#261](https://github.com/cmeans/mcp-awareness/issues/261), [#238](https://github.com/cmeans/mcp-awareness/issues/238).
- **Regconfig validation cache** — `PostgresStore` caches valid Postgres regconfig names from `pg_ts_config` at startup. Write-time validation falls back to `'simple'` for invalid regconfigs (with one cache-refresh retry in case an extension was installed after startup). Prevents INSERT failures from invalid `language` values reaching the generated `tsv` column. Refs [#260](https://github.com/cmeans/mcp-awareness/issues/260), [#238](https://github.com/cmeans/mcp-awareness/issues/238).
- **Layer 1 hybrid retrieval wiring** — Alembic migration adds `language` (regconfig) and `tsv` (generated tsvector with weighted fields) columns to the `entries` table with GIN index. `semantic_search` SQL rewritten to a hybrid CTE fusing vector (HNSW) and lexical (FTS/GIN) branches via Reciprocal Rank Fusion (k=60). Write tools (`remember`, `add_context`, `learn_pattern`, `remind`) gain optional `language` parameter (ISO 639-1) for explicit language override; auto-detection via lingua-py falls back to `simple`. `update_entry` supports changing an entry's language. `Entry` model carries `language` field (default `"simple"`). Graceful degradation: empty FTS branch when query text is short/stop-words-only; empty vector branch when no embeddings exist. Refs [#238](https://github.com/cmeans/mcp-awareness/issues/238).
- **Language resolution helpers** — `src/mcp_awareness/language.py` — pure module providing the resolution chain used at write time and query time for Layer 1 of hybrid retrieval (explicit ISO 639-1 → user preference → `lingua-py` auto-detection → `simple` fallback). ISO 639-1 ↔ Postgres `regconfig` mapping covers the 28 stock snowball-based configurations built into Postgres. CJK and Hebrew support is intentionally deferred pending verification of an appropriate Postgres parser extension — context7 verification during this PR's QA cycle showed that pgroonga's documented integration is its own PostgreSQL index access method (`USING pgroonga`), not the standard `regconfig` registry that the design pattern assumes; zhparser is a verified counter-example for Chinese (parser-extension approach via `CREATE TEXT SEARCH CONFIGURATION ... (PARSER = zhparser)`), but Japanese / Korean / Hebrew equivalents are not yet identified. See [#249](https://github.com/cmeans/mcp-awareness/issues/249) for the three-option trilemma (per-language parser extensions, branched pgroonga query path, or deferral to a follow-up phase after Layer 1) and the module docstring's "Cost considerations" section for the full finding. Lazy detector loading so callers that don't exercise detection don't pay the import cost. `lingua-language-detector>=2.0,<3.0` added as a runtime dependency. Refs [#238](https://github.com/cmeans/mcp-awareness/issues/238); tracks follow-ups [#247](https://github.com/cmeans/mcp-awareness/issues/247) (lingua first-call latency mitigation) and [#248](https://github.com/cmeans/mcp-awareness/issues/248) (Postgres-side regconfig memory measurement, blocked on [#249](https://github.com/cmeans/mcp-awareness/issues/249)).
- **Design doc** — `docs/design/hybrid-retrieval-multilingual.md` — three-layer design for hybrid retrieval (vector + Postgres FTS + RRF), multilingual embeddings, and experimental proposition extraction. Closes the framing of [#195](https://github.com/cmeans/mcp-awareness/issues/195) in favor of a lower-blast-radius approach. Tracked by [#238](https://github.com/cmeans/mcp-awareness/issues/238), [#239](https://github.com/cmeans/mcp-awareness/issues/239), [#240](https://github.com/cmeans/mcp-awareness/issues/240). Amended 2026-04-10 after round-1 QA review and a sourcing audit.
- **Design doc** — `docs/superpowers/specs/2026-04-10-trim-write-tool-responses-design.md` — pragmatic interpretation B (drop payload echoes, keep handles) for write-tool response shapes. Pre-cursor for Layer 1 of [#238](https://github.com/cmeans/mcp-awareness/issues/238). Tracks [#243](https://github.com/cmeans/mcp-awareness/issues/243).
- **`TestWriteResponseShapes`** test class — sentinel-scan parametrized over every write tool plus two registry-completeness tests. Catches regressions when a future write tool echoes caller-supplied payload fields. The `ECHO_EXEMPTIONS` registry doubles as the executable spec for what counts as a primary handle vs a payload echo ([#243](https://github.com/cmeans/mcp-awareness/issues/243)).

### Changed
- **README** — document Layer 1 hybrid retrieval features: hybrid `search` tool (RRF fusion), per-entry language support with auto-detection, `get_knowledge` language filter, regconfig validation, unsupported-language alerts, FTS infrastructure. Refs [#271](https://github.com/cmeans/mcp-awareness/issues/271).
- **Design doc** — `docs/design/hybrid-retrieval-multilingual.md` — close Phase 1.05 (extension selection) with **option 3 — defer non-Western language support from Layer 1**. Decision recorded 2026-04-11 after the empirical PG17.9 verification ([#257](https://github.com/cmeans/mcp-awareness/pull/257)) returned a definitive negative on pgroonga regconfig integration, ruling out the original "install pgroonga, use the 4 entries" path. The trilemma was resolved in favor of pragmatic Layer 1 shipping: at 1 month into mcp-awareness development with no public users and no signal on multilingual demand, Layer 1 ships with the 28 stock snowball regconfigs and `simple` as the fallback for everything else. CJK + Hebrew + Thai + Khmer support becomes a deliberate follow-up release when actual demand surfaces. The decision tree (per-language parser extensions, branched-pgroonga path, or external search index) is preserved in the design doc for the future evaluation, with the empirical verification status of each option documented in the "Verified empirical results for future reference" subsection — zhparser confirmed via context7 during [#246](https://github.com/cmeans/mcp-awareness/pull/246), pgroonga 4.0.6 empirically ruled out by [#257](https://github.com/cmeans/mcp-awareness/pull/257)'s PG17.9 verification, Typesense 29.0 empirically tested in a 20-operation spike on 2026-04-11 (see awareness `typesense-spike-2026-04-11` and `~/.local/state/mcp-awareness-typesense-spike/test-results-2026-04-11.md` for the full test matrix), and Meilisearch documented per its official documentation reviewed via context7 against `/meilisearch/documentation` on 2026-04-11 but not empirically tested. Phase 3 (non-Western language extension install) is reframed as a wiring-PR follow-on contingent on demand. The managed-Postgres compatibility section is reframed as contingent on Phase 3 reactivation. Closes [#249](https://github.com/cmeans/mcp-awareness/issues/249) (gating question answered, mechanism chosen) and [#248](https://github.com/cmeans/mcp-awareness/issues/248) (original premise — measure pgroonga regconfig memory cost — moot since those regconfigs do not exist; surviving stock-snowball measurement scope deferred as below-the-line for current scale).
- **Design doc** — `docs/design/hybrid-retrieval-multilingual.md` — record the empirical PG17.9 verification results for Steps 0 and 1 of the schema verification task ([#249](https://github.com/cmeans/mcp-awareness/issues/249)). **Step 0 (Substantive 3, gating): pgroonga 4.0.6 does not register any regconfigs in `pg_ts_config`** — verified by capturing `SELECT cfgname FROM pg_ts_config` before and after `CREATE EXTENSION pgroonga` against `groonga/pgroonga:latest-alpine-17`; both queries returned the same 29 rows (28 stock snowball + `simple`). `to_tsvector('japanese', '...')` errors with `text search configuration "japanese" does not exist`. The pgroonga extension is functional under its documented integration model (`USING pgroonga` index access method + `&@` operator successfully indexes/queries Japanese and Chinese content); the regconfig absence is by design, not a packaging bug. **Step 1 (Substantive 2, generated-column pattern): works on PG17.9** — `tsv tsvector GENERATED ALWAYS AS (to_tsvector(language, content)) STORED` is accepted at `CREATE TABLE`, populates correctly per row's regconfig, regenerates dynamically when `language` is updated, works with a standard GIN index (`Bitmap Index Scan` confirmed via `EXPLAIN ANALYZE` with `enable_seqscan=off`), and fails at INSERT time when handed a missing regconfig — exactly the case the startup-cache validation is designed to catch. The trigger-based fallback is therefore not needed for the wiring PR (kept in the design doc as documented escape hatch). One Step 1 checkbox remains open: confirming the combined hybrid CTE plan uses both HNSW and GIN indexes (requires a `pgvector` + chosen-non-Western-FTS image, deferred to the wiring PR). Step 2 (#248 memory measurement) and Step 3 (RDS compatibility) remain open and now contingent on Phase 1.05's mechanism choice. **Phase 1.05 (extension selection) is now the load-bearing open decision** — the original "install pgroonga, use the 4 entries" path is empirically ruled out, leaving the three documented options: per-language parser extensions like zhparser, pgroonga with a branched query path, or deferral of non-Western support from Layer 1.
- **Design doc** — `docs/design/hybrid-retrieval-multilingual.md` — record the pgroonga regconfig finding from [#246](https://github.com/cmeans/mcp-awareness/pull/246)'s QA cycle (rounds 3–5): pgroonga's documented integration is its own PostgreSQL index access method, not the standard `regconfig` registry the Layer 1 design assumes. Layer 1's verification task (Substantive 2) is now gated on a new Substantive 3 task (Step 0 of the revised verification): does pgroonga even register the assumed regconfigs in `pg_ts_config`? Tracked as [#249](https://github.com/cmeans/mcp-awareness/issues/249); [#248](https://github.com/cmeans/mcp-awareness/issues/248) (Postgres memory cost) is now blocked on [#249](https://github.com/cmeans/mcp-awareness/issues/249). Adds zhparser as a verified counter-example proving the design pattern (regconfig → tsvector → GIN → standard FTS operators) works for non-Western languages with the right extension, but only for Chinese; Japanese / Korean / Hebrew equivalents are not yet verified. Defers non-Western FTS mechanism selection to the wiring PR (new Phase 1.05) with three explicit options: per-language parser extensions, pgroonga with a branched query path, or deferral from Layer 1. Phase 3 (non-Western language extension install) is reframed to cover all three options. Risk section and managed-Postgres compatibility analysis updated to reflect that extension choice is open.
- **perf:** trim echoed input from write-tool responses to reduce token waste ([#243](https://github.com/cmeans/mcp-awareness/issues/243)). Five tools change; eight retain handles or server-derived fields only. Static `action` strings are dropped because they carry zero information on tools whose value is hard-coded.
  - `learn_pattern` no longer echoes `description`; now returns `{status, id}`
  - `remember` no longer echoes `description`; now returns `{status, id}` on the normal path, or `{status, id, action}` (`created`|`updated`) when `logical_key` is provided. Presence of `action` itself signals the upsert path was taken.
  - `set_preference` no longer echoes `value`; now returns `{status, id, key, scope}`. The new `id` field is the entry id of the stored preference (captured from `upsert_preference`); `key`+`scope` are retained as the compound upsert handle.
  - `acted_on` no longer echoes `platform`/`detail`/`tags`; now returns `{status, id, entry_id, action, timestamp}`. `action` is the caller-supplied effect label (the substance of the action record), kept as documentation of what the call recorded.
  - `update_intention` no longer echoes `state` or `reason`; now returns `{status, id}`. Verified in code that `state` was a pure pass-through with no coercion or auto-advancement, making it textbook echoed input.
  - **Breaking for clients that read these fields from write responses.** The data is trivially recoverable (caller already has it). The eight unchanged write tools (`report_status`, `report_alert`, `update_entry`, `suppress_alert`, `add_context`, `delete_entry`, `restore_entry`, `remind`) keep their existing shapes — handles or server-derived fields only.

### Fixed
- **Alembic DSN format handling** — `alembic/env.py` now converts psycopg DSN format (`host=X dbname=Y user=Z password=W`) to SQLAlchemy URL format via `dsn_to_sqlalchemy_url()` helper. Delegates DSN parsing to `psycopg.conninfo.conninfo_to_dict()` for correctness; forwards extra params (sslmode, connect_timeout, etc.) as URL query string. Fixes migration/backfill failures on production where `AWARENESS_DATABASE_URL` uses DSN format.
- **Deploy script** — `scripts/holodeck/deploy.sh` maintenance mode no longer passes `upgrade head` positional args to `mcp-awareness-migrate` (which uses `--flags`, not positional args).
- **README** — fix documented `mcp-awareness-migrate upgrade head` syntax to match actual CLI interface (`mcp-awareness-migrate` with no positional args).
- **Docs** — document that `AWARENESS_DATABASE_URL` accepts both URL and DSN formats, and that DSN values must be quoted in env files to prevent shell space-splitting. Updated in README, data dictionary, `migrate.py` error message, and `alembic/env.py` error message.

## [0.16.2] - 2026-04-09

### Added
- **Stateless HTTP mode** — opt-in via `AWARENESS_STATELESS_HTTP=true`. Creates a fresh MCP transport per request with no session tracking, eliminating the entire class of session drop / 409 Conflict bugs ([#180](https://github.com/cmeans/mcp-awareness/issues/180)). Auth still flows per-request via JWT Bearer token. Session registry is automatically skipped in stateless mode. Stateful mode (default) remains available for clients that need persistent sessions.
- **MCP request logger** — logs method, truncated session ID, client IP, and response status for every `/mcp` request. Placed outside the session registry for full visibility into both intercepted and pass-through requests.

### Fixed
- `_cleanup_expired` now RLS-safe and opt-in — scoped by `owner_id` instead of bypassing row-level security. Only runs for owners with `auto_cleanup=true` preference. Default: no cleanup — expired entries retained until user opts in. Handles both active expiry and trash retention ([#179](https://github.com/cmeans/mcp-awareness/issues/179), [#183](https://github.com/cmeans/mcp-awareness/issues/183))
- `delete_entry` IDOR fix — single-entry delete by ID now returns `"status": "acknowledged"` with no count, preventing entry existence enumeration across tenants. Bulk deletes (tags, source) retain counts since they're already owner-scoped ([#193](https://github.com/cmeans/mcp-awareness/issues/193))
- Session registry now intercepts `GET /mcp` (SSE reconnect) — previously only POST and DELETE were handled, causing stale GET requests to bypass re-initialization and return 409 directly from FastMCP ([#178](https://github.com/cmeans/mcp-awareness/issues/178))
- `_LazyStore` thread safety — added double-checked locking to prevent duplicate `PostgresStore`/connection pool creation under concurrent access from embedding workers, cleanup thread, or parallel requests ([#164](https://github.com/cmeans/mcp-awareness/issues/164))
- SQL template injection hardening — replaced `str.format()` with `psycopg.sql.SQL` composition across all 13 dynamic query sites in `postgres_store.py`, enforced via `psql.Composable` types that mypy validates at the call boundary ([#165](https://github.com/cmeans/mcp-awareness/issues/165))
- `get_unread(since=...)` param ordering bug — `since` value was passed as `owner_id` and vice versa due to SQL placeholder position mismatch (pre-existing, discovered during #165 coverage work)

## [0.16.1] - 2026-04-09

### Fixed
- Per-owner concurrency limit now configurable via `AWARENESS_MAX_CONCURRENT_PER_OWNER` (default raised from 3 to 10) — Claude.ai sends parallel MCP requests that exceeded the old limit, causing 429 errors surfaced as "authorization failed"
- OAuth proxy rate limits now configurable via `AWARENESS_OAUTH_PROXY_RATE_{AUTHORIZE,TOKEN,REGISTER}` (defaults raised from 20/10/5 to 60/60/30 req/min) and `AWARENESS_OAUTH_PROXY_RATE_WINDOW` (sliding window, default 60s)
- SessionRegistryMiddleware now compatible with MCP SDK 1.27.0 SSE responses — `_buffer_body` forwards to real `receive` after replay, `_handle_subsequent` streams 2xx responses immediately, `_reinitialize` blocks until task group cancellation for SSE disconnect detection
- Em dash in session SQL comment replaced with ASCII — `psycopg.sql.SQL` rejected the non-ASCII character
- `deploy.sh` now aborts on first node health check failure instead of continuing to the next node
- UTF-8 encoding enforced on all database creation paths — `CREATE DATABASE` uses `ENCODING 'UTF8' TEMPLATE template0` with locale `C` for portability; `pg_hba.conf` documented with single `host all awareness` rule
- Runtime guard against `{}` in SQL comments that broke `psycopg.sql.SQL.format()` placeholder detection

## [0.16.0] - 2026-04-08

### Added
- **Session persistence** — Postgres-backed session registry survives node restarts and rolling deploys. ASGI middleware transparently re-initializes MCP sessions on cross-node recovery with redirect table for session continuity. Feature-gated by `AWARENESS_SESSION_DATABASE_URL` ([#161](https://github.com/cmeans/mcp-awareness/issues/161))
- Per-owner session limits (configurable via `AWARENESS_MAX_SESSIONS_PER_OWNER`, default 10)
- Session touch debounce (sliding-window TTL, updates at most once per 30 seconds)
- Graceful degradation when session database is unreachable
- Auto-create session database on startup (requires `CREATEDB` privilege)

## [0.15.0] - 2026-04-07

### Added
- **OAuth 2.1 resource server**: provider-agnostic JWKS-based token validation for external OAuth providers (WorkOS, Auth0, Cloudflare Access, Keycloak, etc.)
- **Dual auth**: self-signed JWTs (via CLI) and OAuth provider tokens both accepted — OAuth for interactive clients, self-signed for edge providers/scripts
- **User auto-provisioning**: auto-create user record on first valid OAuth login (`AWARENESS_OAUTH_AUTO_PROVISION`, default: false for tighter control during early access)
- **Well-known metadata**: `/.well-known/oauth-protected-resource` (RFC 9728) for OAuth discovery by MCP clients
- **OAuth env vars**: `AWARENESS_OAUTH_ISSUER`, `AWARENESS_OAUTH_AUDIENCE`, `AWARENESS_OAUTH_JWKS_URI`, `AWARENESS_OAUTH_USER_CLAIM`, `AWARENESS_OAUTH_AUTO_PROVISION`
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
- **OAuth proxy workaround**: feature-gated middleware (`AWARENESS_OAUTH_PROXY=true`) that proxies `/authorize`, `/token`, `/register` to the external OAuth provider (e.g. WorkOS) — works around Claude Desktop/Claude.ai bugs that ignore external auth endpoints
- **OAuth proxy rate limiting**: per-IP sliding window rate limits with auto-ban for bogus requests (injection patterns, wrong HTTP methods, missing required params)
- **OAuth proxy health stats**: `/health` endpoint includes `oauth_proxy` section with completed flows, raw hits, rate-limited counts, and banned IP counts — enables detection of when upstream bugs are fixed
- **Configurable IP resolution**: `AWARENESS_OAUTH_PROXY_IP_HEADERS` env var for infrastructure-portable client IP detection (default: `CF-Connecting-IP,X-Real-IP`)
- **OAuth staging compose**: `docker-compose.oauth.yaml` for isolated OAuth/WorkOS AuthKit testing with separate Postgres, Cloudflare tunnel, and optional Ollama — runs on port 8421 alongside production
- **OAuth env template**: `.env.oauth.example` with all required/optional variables for staging deployment
- **Gzip response compression**: HTTP transport compresses non-SSE responses over 500 bytes via Starlette GZipMiddleware — applies to health checks and JSON endpoints; MCP tool responses use SSE (`text/event-stream`) which Starlette excludes from compression, so rely on Cloudflare or reverse proxy for tool traffic compression (#112)
- **Did-you-mean suggestions**: enum parameter errors include `suggestion` field and "Did you mean '...'?" in message when the supplied value is within edit distance 2 of a valid value (#142)
- **Help URLs**: timestamp and content-type errors include reference URLs inline in message and as `help_url` field — ISO 8601 (Wikipedia), MIME types (MDN) (#142)
- **Retryable flag**: every error includes `retryable: true/false` so agents know whether to retry or self-correct (#142)
- **JWKS auto-discovery**: when `AWARENESS_OAUTH_JWKS_URI` is not set, the server now fetches `<issuer>/.well-known/openid-configuration` to discover the correct `jwks_uri` before falling back to `<issuer>/.well-known/jwks.json` — fixes WorkOS compatibility (#126)
- **OAuth user profile enrichment**: email and display_name populated from token claims on subsequent logins if missing
- **Userinfo endpoint**: when access tokens lack `email`/`name` claims (e.g. WorkOS AuthKit), the server now calls the provider's OIDC userinfo endpoint to fetch identity fields for user resolution (#125)

### Changed
- **Briefing batch queries**: `generate_briefing` now uses 5 fixed queries instead of 3-4 per source (N+1 → batch), reducing DB round trips from 80+ to 5 for 20 sources
- **Dependency version caps**: all runtime dependencies now have upper-bound constraints (e.g., `mcp[cli]>=1.0.0,<2.0`) to prevent breaking major version upgrades
- **`entries.updated` nullable**: column is now NULL on insert, set only on actual updates — aligns with `users.updated` semantics; sort and filter queries use `COALESCE(updated, created)` for consistency
- **Default query limit reduced**: `DEFAULT_QUERY_LIMIT` lowered from 200 to 100 — reduces default response size for all paginated tools (#112)
- **Pagination metadata**: all paginated tools now return `{entries, limit, offset, has_more}` instead of a bare list — agents can detect when more data exists without a separate count query (#112)
- **Structured error responses**: all tool errors now return `{"status": "error", "error": {"code", "message", "retryable", ...}}` with contextual fields (`param`, `value`, `valid`, `suggestion`, `help_url`) instead of flat strings — enables smart rendering by any consumer (#142)
- **MCP isError flag**: tool errors now raise `ToolError` so the MCP SDK sets `isError: true` on `CallToolResult` — clients that support error styling can use this signal (#142)
- **`upsert_by_logical_key` single-connection refactor**: the INSERT, existing-row fetch, and conditional UPDATE now share a single pooled connection and transaction instead of acquiring up to 3 separate connections, reducing pool contention under concurrency (MEDIUM #2)
- **Richer embedding text for preferences and status entries**: `compose_embedding_text()` now includes key/value/scope for preferences, metrics keys/values and inventory for status entries, and truncates long content to 500 chars — produces higher-quality embeddings for previously sparse entry types (MEDIUM #20)
- **Fired intentions SQL filter**: `get_fired_intentions` now filters by `deliver_at` in the SQL WHERE clause instead of fetching all pending intentions and filtering in Python (MEDIUM #5)
- **Cleanup logging**: replaced `print()` in `_do_cleanup` with `logger.error()` to use the module's logging infrastructure (MEDIUM #6)
- **Bearer scheme case-insensitive**: `AuthMiddleware` now accepts `bearer`, `Bearer`, `BEARER` per RFC 7235
- **`AWARENESS_PUBLIC_URL`**: new env var for `/.well-known/oauth-protected-resource` resource URL — required for Cloudflare tunnel deployments where `0.0.0.0:8420` is not the public address
- **docker-compose.yaml**: auth/OAuth env vars now passed through (AUTH_REQUIRED, JWT_SECRET, OAUTH_ISSUER, etc.)
- **Dockerfile license**: corrected from `Apache-2.0` to `AGPL-3.0-or-later`
- **Per-owner concurrency limit**: `AuthMiddleware` enforces max 3 concurrent requests per owner_id — prevents a single aggressive client from saturating the connection pool and DOSing other tenants (returns 429)
- **Connection pool default**: bumped from 5 to 10 for multi-tenant deployments
- **Sync DB I/O off event loop**: `_try_oauth` and `_resolve_user` now run in `asyncio.to_thread()` to avoid blocking the async event loop with sync psycopg calls

### Fixed
- **JSON content rejected by Pydantic validation**: `remember` and `update_entry` now accept `dict` and `list` content in addition to `str` — fixes `string_type` validation error when MCP transport auto-parses JSON strings before they reach the handler (#130)
- **Data dictionary: missing OAuth columns**: added `oauth_subject` and `oauth_issuer` columns to users table documentation (MEDIUM #24)
- **Data dictionary: missing OAuth indexes**: added `ix_users_oauth_identity` and `ix_users_oauth_subject` indexes to users table documentation (MEDIUM #25)
- **Data dictionary: missing intention state**: added `active` to intention `state` field's valid values to match `schema.py` INTENTION_STATES (MEDIUM #26)
- **Data dictionary: entries `updated` nullability**: corrected `updated` column from NOT NULL to nullable, matching the actual schema (MEDIUM #27)
- **Undocumented `AWARENESS_PUBLIC_URL`**: added to README, auth-setup.md, and data dictionary — required for correct `/.well-known/oauth-protected-resource` URLs behind reverse proxies (MEDIUM #28)
- **Ollama response validation**: `OllamaEmbedding.embed()` now validates that the number of returned embeddings matches the number of input texts, raising `ValueError` on partial responses instead of silently dropping entries via `zip(strict=False)` (MEDIUM #21)
- **Embedding upsert preserves `created`**: `upsert_embedding.sql` no longer overwrites the `created` timestamp on conflict — only the vector, hash, and dimensions are updated (MEDIUM #8)
- **Alert expiry filter**: `get_active_alerts` and `get_all_active_alerts` now filter out expired alerts (`expires > NOW()`), matching the behavior of `get_active_suppressions` (MEDIUM #18)
- **Intention lifecycle**: `generate_briefing` now transitions fired intentions from "pending" to "fired" state, preventing them from firing on every subsequent briefing read
- **Custom prompt sync uses DEFAULT_OWNER**: `_sync_custom_prompts` now queries `DEFAULT_OWNER` instead of the request-scoped `_owner_id()`, preventing User A's prompt sync from leaking into User B's prompt registry in multi-tenant deployments (MEDIUM #14)
- **Custom prompt sync debounce**: `_sync_custom_prompts` now skips the DB query if called again within 60 seconds, avoiding a round-trip on every `agent_instructions` invocation (MEDIUM #15)
- **`semantic_search` empty-string guard**: add missing empty-string validation for `since` and `until` parameters — passing `""` now returns a clear error instead of a `ValueError` (MEDIUM #16)
- **`update_intention_state` owner isolation**: SQL WHERE clause now enforces `owner_id`, consistent with all other UPDATE statements (defense-in-depth alongside RLS)
- **`upsert_alert` race condition**: rewritten to use single connection with `pg_advisory_xact_lock` + `SELECT FOR UPDATE`, eliminating TOCTOU duplicate/lost-update window
- **`upsert_preference` race condition**: rewritten to use single connection with `pg_advisory_xact_lock` + `SELECT FOR UPDATE`, eliminating TOCTOU duplicate/lost-update window
- **Date validation**: all tools now return structured JSON errors for malformed date parameters instead of crashing with `ValueError`
- **Global patterns**: patterns with empty source are now applied during briefing generation, matching existing global suppression behavior
- **PR label automation**: `Dev Active` is now a proper hold state — `on-push` and `on-ci-pass` skip pipeline transitions while it's present, `on-unlabel` handles promotion when it's removed
- **PR label automation**: `on-ci-pass` no longer fails on force-pushed PRs — `gh api` 404 errors handled gracefully
- **PR label automation**: removing `Dev Active` checks CI status (via workflow runs API, job-name-agnostic) and promotes to `Ready for QA` or `Awaiting CI` accordingly
- **PR label automation**: adding `Dev Active` now also clears `Awaiting CI` and `Ready for QA` to prevent competing state
- **PR label automation**: added explicit `checks: read` permission
- **PR label automation**: `on-ci-pass` now finds PRs from dependabot and other non-default branches by falling back to head branch search when the `pull_requests` array is empty

### Removed
- **Dead code**: removed unused `validate_entry_data` function from `schema.py` and its tests (MEDIUM #17)

### Security
- **JWT issued-at validation**: both self-signed and OAuth token paths now reject tokens with future `iat` claims via `verify_iat: True` — prevents acceptance of not-yet-valid tokens
- **Parameterized LIMIT clauses**: `get_reads`, `get_actions`, and `get_activity` now use bind parameters (`%s`) for LIMIT values instead of f-string interpolation, eliminating a fragile SQL construction pattern (MEDIUM #3)
- **`cleanup_expired` RLS-safe**: background cleanup now uses `SET LOCAL row_security = off` so expired entries are cleaned regardless of RLS enforcement
- **`clear()` scoped to owner**: `clear(owner_id)` deletes only that owner's data instead of truncating all tenants
- **Argon2 time_cost bumped to 3**: stronger password hashing for new and changed passwords (existing hashes remain valid)
- **Auth exception logging**: `_try_oauth` and `_resolve_user` now log warnings on failure instead of silently swallowing exceptions — operators get visibility into OAuth/user-resolution errors
- **Password hash excluded from GDPR export**: `mcp-awareness-user export` now uses explicit column list instead of `SELECT *` — password hashes are no longer included in export output
- **Semantic search limit clamped**: `semantic_search` limit parameter now clamped to 1–100 range, preventing unbounded result sets
- **JWKS cache thread-safe**: OAuth token validator now uses a threading lock with double-check pattern to prevent thundering herd on cache refresh
- **DDL uses `psycopg.sql.Literal`**: default owner value in `CREATE TABLE` DDL now uses proper SQL escaping via `psycopg.sql` instead of manual string replacement
- **FORCE ROW LEVEL SECURITY**: RLS policies now enforced on table owner role — previously `ENABLE` without `FORCE` allowed the connection pool role to bypass all policies
- **UPDATE SQL owner scoping**: `update_entry`, `upsert_alert_update`, `upsert_preference_update` now include `AND owner_id = %s` in WHERE clause — prevents cross-tenant updates
- **OAuth canonical_email matching**: auto-provisioning and identity linking now use `canonical_email` (strips Gmail dots/+tags) — prevents duplicate accounts from email variants
- **AuthMiddleware default**: `auto_provision` parameter defaults to `False` (was `True`) — prevents accidental auto-provisioning when instantiated directly

### Documentation
- **Migration backfill notes**: added performance advisory comments to `f1a2b3c4d5e6` (owner_id backfill) and `h3c4d5e6f7g8` (updated nullability backfill) migrations — for large tables (>100K rows), includes a batched UPDATE example to avoid long-held locks (MEDIUM #7)
- **Hash stability**: documented embedding hash behavior in `embeddings.py` module docstring and function docstrings — explains that changes to `compose_embedding_text()` invalidate all stored hashes and trigger mass re-embedding (MEDIUM #22)
- **Data dictionary**: expanded `text_hash` column description in `docs/data-dictionary.md` to explain staleness detection and mass re-embedding on composition changes
- **Auth setup guide** (`docs/auth-setup.md`): JWT authentication, OAuth 2.1, CLI tools reference, user provisioning, WorkOS walkthrough, known limitations
- **README**: auth/OAuth env vars tables, CLI tools, security section rewritten (4-layer table), test count 383→490, removed stale "not yet implemented" auth line
- **CLAUDE.md**: architecture file tree updated with all 16 modules (added tools.py, resources.py, prompts.py, helpers.py, migrate.py, instructions.md, sql/), server.py description corrected, mcp-awareness-migrate CLI added
- **Deployment guide**: security section updated for JWT/OAuth, license footer fixed (Apache 2.0 → AGPL-3.0)
- **All docs**: branded footer with logo, consistent copyright format

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

[Unreleased]: https://github.com/cmeans/mcp-awareness/compare/v0.16.2...HEAD
[0.16.2]: https://github.com/cmeans/mcp-awareness/compare/v0.16.1...v0.16.2
[0.16.1]: https://github.com/cmeans/mcp-awareness/compare/v0.16.0...v0.16.1
[0.16.0]: https://github.com/cmeans/mcp-awareness/compare/v0.15.0...v0.16.0
[0.15.0]: https://github.com/cmeans/mcp-awareness/compare/v0.14.0...v0.15.0
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
