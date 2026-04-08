<!-- SPDX-License-Identifier: AGPL-3.0-or-later | Copyright (C) 2026 Chris Means -->
# MCP Session Persistence Design

> **GitHub Issue:** #161
> **Status:** Design approved, ready for implementation planning
> **Date:** 2026-04-07

## Problem

MCP sessions are stored in-memory (Python dict in FastMCP's
`StreamableHTTPSessionManager`). When a node restarts during a rolling deploy,
all sessions on that node are lost. Clients get "Session terminated" errors and
must manually reconnect. This also kills Claude Code's awareness MCP connection
after every deploy, requiring `/mcp` to reconnect.

No one in the MCP ecosystem has solved this with Postgres. The community uses
Redis workarounds or sticky sessions. The official spec plans to address
distributed sessions in the June 2026 release. We are ahead of the curve.

## Approach

**Option A + C: Session resumability via middleware + upstream contribution.**

We do not serialize FastMCP's live async state (task groups, memory streams).
Instead, we maintain a Postgres-backed **session registry** that distinguishes
"session died because of a deploy" (recoverable) from "session never existed"
(404). A middleware layer transparently re-initializes sessions on the new node.

After the implementation is proven, contribute a pluggable `SessionStore`
protocol to the MCP Python SDK (issue #880).

## Architecture

### Two-layer design

1. **Session Registry** (Postgres) — tracks which sessions exist, who owns them,
   and what capabilities were negotiated. Source of truth for session validity.

2. **Session Registry Middleware** (ASGI) — intercepts requests between
   AuthMiddleware and FastMCP. Checks the registry and handles cross-node
   re-initialization transparently.

### Request flow

```
Client → Cloudflare → HAProxy → CT 210 or 211
  → SecretPathMiddleware
  → WellKnownMiddleware
  → AuthMiddleware (JWT → owner_id)
  → SessionRegistryMiddleware  ← NEW
  → FastMCP StreamableHTTPSessionManager (in-memory sessions)
  → Tools/Resources
```

### Request handling

**Initialize (no session ID):**

1. Check `count_active(owner_id)` — reject with 429 if at session limit
2. Pass request through to FastMCP
3. Capture `mcp-session-id` from response headers
4. Register session in Postgres (session_id, owner_id, node, capabilities,
   client_info)

**Subsequent request (session ID present):**

1. `lookup(session_id)` in Postgres (includes `WHERE expires_at > NOW()` —
   expired sessions are treated as not found, same as truly invalid ones)
2. If not found → check `redirect_lookup(session_id)` for old→new mapping
   (see session continuity below). If redirect found, rewrite request header
   to new session_id and continue at step 3. If no redirect → pass through
   to FastMCP (returns 404)
3. If found, validate `owner_id` matches JWT → reject 403 on mismatch
4. Pass request through to FastMCP
5. If FastMCP returns success → `touch()` (debounced, also extends
   `expires_at = NOW() + TTL` for sliding-window expiry), done
6. If FastMCP returns 404 (session not in local memory) → **re-initialize**
   (see below)

**Re-initialization (cross-node recovery):**

When a session exists in the registry but not in FastMCP's local memory (node
restart or HAProxy routed to a different node), the middleware performs a
two-step re-initialization:

1. Build a synthetic `initialize` request using stored metadata from the
   registry: `capabilities` and `client_info` columns provide the original
   handshake parameters, `protocol_version` provides the negotiated version
2. Send the synthetic `initialize` to FastMCP → FastMCP creates a new local
   session and returns a new `mcp-session-id`
3. Register the new session_id in Postgres
4. Store a redirect mapping: old_session_id → new_session_id (see session
   continuity below)
5. Invalidate the old session_id in the registry (mark expired, not deleted —
   the redirect mapping references it)
6. Replay the original request (e.g., `tools/call`) with the new session_id
7. Return the response to the client with the new `mcp-session-id` header

**Session continuity after rotation:**

After re-init, the client still holds the old session_id. To avoid breaking
the client on its next request:

- The middleware maintains a **redirect table** (`session_redirects`) mapping
  old_session_id → new_session_id with a grace period (default: 5 minutes)
- When a request arrives with an old session_id that has a redirect, the
  middleware transparently rewrites the request header to the new session_id
- The response includes the new `mcp-session-id` header so the client can
  update (if the client supports it — Claude Desktop/Claude.ai may not)
- After the grace period, the redirect expires and the old session_id becomes
  truly invalid

Redirect table schema (same database):

```sql
CREATE TABLE session_redirects (
    old_session_id  TEXT PRIMARY KEY,
    new_session_id  TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at      TIMESTAMPTZ NOT NULL  -- created_at + 5 min
);
```

**HAProxy stick table after rotation:**

After re-init issues a new session_id, HAProxy's stick table has no entry for
it. The `stick store-response` rule captures it from the re-init response, but
there is a brief window where the next request may round-robin to the wrong
node and trigger another re-init. This is self-healing (the second re-init
succeeds and a new stick entry is created) but operators should expect a brief
burst of re-inits (2-3 per session) immediately after a deploy. This settles
within seconds.

**Terminate (DELETE /mcp):**

1. `invalidate(session_id)` in Postgres
2. Delete any redirect mappings pointing to this session
3. Pass through to FastMCP for local cleanup

### Key design decisions

- **No FastMCP internals dependency.** We detect "session not on this node" by
  catching FastMCP's 404 response, not by inspecting `_server_instances`.
- **Synthetic initialize for re-init.** Most re-inits are triggered by
  `tools/call` or similar, not `initialize`. The middleware sends a synthetic
  `initialize` using stored registry metadata before replaying the original
  request. FastMCP always sees a valid handshake sequence.
- **Redirect table for session continuity.** Clients holding old session_ids
  are transparently redirected to the new session_id for a 5-minute grace
  period. Avoids breaking clients that don't update their session_id from
  response headers.
- **Sliding-window TTL.** `touch()` extends `expires_at` on each request, so
  active sessions never expire. Only idle sessions expire after TTL. This
  matches the "30 minutes of inactivity" intent from issue #161.
- **lookup() filters expired sessions.** Expired-but-not-yet-cleaned sessions
  are treated as "not found" — no re-init path, clean 404. This prevents
  zombie sessions from accumulating via re-init loops.
- **Feature-gated.** Disabled unless `AWARENESS_SESSION_DATABASE_URL` is set.
  Zero behavioral change for stdio or single-node deployments.
- **Graceful degradation.** If the session database is unreachable, the
  middleware logs an error and passes requests through unmodified, falling back
  to FastMCP's in-memory behavior.
- **LOGGED tables (deliberate deviation from issue #161).** Issue suggested
  UNLOGGED for performance. Spec chose LOGGED because write volume is low
  (single-digit sessions/minute) and WAL overhead is negligible. Can revisit
  if write volume increases significantly.

## Database

### Separate database

`awareness_sessions` on CT 200 (same Postgres instance, separate database).
Keeps ephemeral session data out of the main `awareness` database — cleaner
backups, independent retention, separate connection pool.

### Schema

```sql
CREATE TABLE session_registry (
    session_id       TEXT PRIMARY KEY,
    owner_id         TEXT NOT NULL,
    node             TEXT,
    protocol_version TEXT,
    capabilities     JSONB NOT NULL DEFAULT '{}',
    client_info      JSONB NOT NULL DEFAULT '{}',
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at       TIMESTAMPTZ NOT NULL
);

CREATE INDEX ix_session_registry_expires ON session_registry (expires_at);
CREATE INDEX ix_session_registry_owner ON session_registry (owner_id);
```

### Design decisions

- **LOGGED tables** — survives Postgres crash. Write volume is low (handful of
  sessions per minute), WAL overhead is negligible.
- **No RLS** — session lookup is by opaque session_id. Ownership validated at the
  application layer (middleware checks JWT owner matches session owner_id).
- **No Alembic** — simple DDL via `ensure_schema()` (idempotent
  `CREATE TABLE IF NOT EXISTS`). Ephemeral schema not worth migration
  infrastructure.
- **`node` column** — informational for debugging and metrics, not used for
  routing.
- **`expires_at` computed at insert** — `NOW() + interval` based on
  `AWARENESS_SESSION_TTL`.
- **`last_seen` debounced** — updated at most once per 30 seconds per session
  (application-level in-memory timestamp check).

## Security

### Data exposure

| Field | Content | PII risk |
|-------|---------|----------|
| session_id | uuid4().hex (128-bit random) | None |
| owner_id | OAuth `sub` claim (opaque WorkOS ID) | Pseudonymous |
| capabilities | MCP protocol metadata | None |
| client_info | `{"name": "claude-desktop", "version": "1.0"}` | None |

No awareness entries, knowledge, or personal data touches this database.

### Threat model

| Threat | Mitigation |
|--------|------------|
| Session flooding (spam initialize) | Per-owner session count limit (configurable, default 10). AuthMiddleware requires valid JWT. Per-owner concurrency limit (3) caps creation rate. |
| Session probing (guess IDs) | uuid4().hex = 128 bits of randomness. Owner_id validation: even with a valid session_id, JWT must match the session's owner. |
| Session fixation | New session_id issued on cross-node re-init. Old ID invalidated. |
| Expired session accumulation | Background cleanup thread purges expired rows (same pattern as main store). |
| Registry as DoS amplifier | Separate connection pool (max 5) bounds DB load. Per-owner concurrency limit applies. |
| Session DB unreachable | Graceful degradation: fall back to in-memory behavior, log error. |

### Alerting on session limits

When a user hits the session limit:

1. Log warning: `"Owner {owner_id} at session limit ({count}/{max})"`
2. Write awareness alert (source `"mcp-awareness"`, severity `"warning"`) so
   it surfaces in briefings
3. Return 429 with message: `"Session limit reached (N/N). Contact admin if
   this is unexpected."`

The limit is configurable via `AWARENESS_MAX_SESSIONS_PER_OWNER` to support
load testing without code changes.

## Implementation

### New file: `src/mcp_awareness/session_registry.py`

Two classes:

**`SessionStore`** — Postgres client for the session registry:

- `register(session_id, owner_id, node, protocol_version, capabilities, client_info) -> None`
- `lookup(session_id) -> dict | None` (filters `expires_at > NOW()`)
- `touch(session_id) -> None` (debounced: updates `last_seen` and extends `expires_at`)
- `invalidate(session_id) -> None`
- `count_active(owner_id) -> int`
- `add_redirect(old_session_id, new_session_id) -> None` (5-min grace period)
- `redirect_lookup(session_id) -> str | None` (returns new_session_id if redirect exists and not expired)
- `cleanup_expired() -> int` (purges expired sessions and expired redirects)
- `ensure_schema() -> None` (idempotent DDL for both tables)

Uses `psycopg_pool.ConnectionPool` (min 1, max 5). Follows the same patterns
as `PostgresStore`: connection context manager, transaction scope, background
cleanup thread with debounce.

**`SessionRegistryMiddleware`** — ASGI middleware:

- `__init__(self, app, session_store, node_name, max_sessions_per_owner=10)`
- Intercepts POST/DELETE to `/mcp`
- Handles initialize capture, cross-node re-init, terminate cleanup
- Touch debounce via in-memory dict of `{session_id: last_touch_time}`

### Integration point: `server.py`

In the streamable-http transport setup (~line 505), mount the middleware
between AuthMiddleware and the FastMCP app:

```python
if SESSION_DATABASE_URL:
    session_store = SessionStore(SESSION_DATABASE_URL, ...)
    app = SessionRegistryMiddleware(app, session_store, node_name=NODE_NAME)
```

## Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `AWARENESS_SESSION_DATABASE_URL` | (none) | Postgres DSN for session database. If unset, registry disabled. |
| `AWARENESS_SESSION_TTL` | `1800` | Session expiry in seconds (30 min) |
| `AWARENESS_SESSION_POOL_MIN` | `1` | Min connection pool size |
| `AWARENESS_SESSION_POOL_MAX` | `5` | Max connection pool size |
| `AWARENESS_MAX_SESSIONS_PER_OWNER` | `10` | Active session limit per owner |
| `AWARENESS_SESSION_NODE_NAME` | hostname | Identifies this node in the registry |

## Deployment (holodeck)

1. Verify pg_hba.conf on CT 200 allows `awareness` user to access `all` databases
   (needed for both `awareness_sessions` and `postgres` for auto-create).
   If using per-database rules, add entries for `awareness_sessions` and `postgres`.
2. Create database on CT 200 (or let `_ensure_database` auto-create on first startup):
   ```sql
   CREATE DATABASE awareness_sessions OWNER awareness ENCODING 'UTF8' LC_COLLATE 'C.UTF-8' LC_CTYPE 'C.UTF-8' TEMPLATE template0;
   ```
3. Add env vars to CT 210 and CT 211:
   ```bash
   AWARENESS_SESSION_DATABASE_URL=postgresql://awareness:<pw>@192.168.200.100:5432/awareness_sessions
   AWARENESS_SESSION_NODE_NAME=app-a  # or app-b
   ```
4. Hot deploy — middleware auto-creates schema on first startup

## Testing

### Unit tests (~20-25 new tests in `tests/test_session_registry.py`)

- SessionStore CRUD: register, lookup, touch, invalidate, count_active,
  cleanup_expired
- Session limit enforcement: reject at limit
- Owner mismatch: middleware rejects when JWT owner differs from session
- Touch debounce: last_seen only updates after 30s gap
- Schema idempotence: ensure_schema() safe to call repeatedly
- Expiry: expired sessions not returned by lookup, cleaned by cleanup

### Integration tests

- Same-node flow: initialize → tool call → same session reused
- Cross-node simulation: register on store A → lookup from store B →
  re-initialize triggers, new session_id issued, old invalidated
- Graceful degradation: session DB unreachable → requests pass through
- Alert on limit: hit session count cap → verify awareness alert
- Terminate: DELETE /mcp → session removed from registry

### Manual QA (infrastructure tests)

- Hot deploy with active Claude Desktop session → reconnect is seamless
- HAProxy routes to "wrong" node → transparent re-initialize
- Kill one app node → other node picks up sessions

## Future work

### Phase 2: Full session migration (Option B)

When SLA requirements demand zero-interruption:

- Implement Postgres-backed `EventStore` (the MCP SDK interface)
- Store every `JSONRPCMessage` with stream_id + event_id
- On reconnect with `Last-Event-ID`, replay missed events
- Builds on the `awareness_sessions` database infrastructure from Phase 1

### Upstream contribution

1. Comment on modelcontextprotocol/python-sdk#880 with approach and results
2. Propose pluggable `SessionStore` protocol — abstract interface with
   `InMemorySessionStore` as default
3. Offer `PostgresSessionStore` as reference implementation
4. Blog post: "MCP Session Persistence with Postgres"

### Spec alignment

The MCP spec roadmap targets June 2026 for session/transport evolution. Our
middleware approach is compatible with the spec direction (stateless protocol
with explicit session mechanisms). When the spec lands, we adapt our middleware
to the standard interface.
