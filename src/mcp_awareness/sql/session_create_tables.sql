/* name: session_create_tables */
/* mode: literal */
/* Create session_registry and session_redirects tables with indexes.
   Runs on startup if AWARENESS_SESSION_DATABASE_URL is set.
   Uses IF NOT EXISTS — safe to run repeatedly (idempotent).
*/
CREATE TABLE IF NOT EXISTS session_registry (
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

CREATE INDEX IF NOT EXISTS ix_session_registry_expires
    ON session_registry (expires_at);
CREATE INDEX IF NOT EXISTS ix_session_registry_owner
    ON session_registry (owner_id);

CREATE TABLE IF NOT EXISTS session_redirects (
    old_session_id  TEXT PRIMARY KEY,
    new_session_id  TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at      TIMESTAMPTZ NOT NULL
);
