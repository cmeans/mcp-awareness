-- _create_tables: DDL for all tables (users, entries, reads, actions, embeddings)
CREATE TABLE IF NOT EXISTS users (
    id              TEXT PRIMARY KEY,
    email           TEXT,
    canonical_email TEXT UNIQUE,
    email_verified  BOOLEAN NOT NULL DEFAULT FALSE,
    phone           TEXT,
    phone_verified  BOOLEAN NOT NULL DEFAULT FALSE,
    password_hash   TEXT,
    display_name    TEXT,
    timezone        TEXT DEFAULT 'UTC',
    preferences     JSONB NOT NULL DEFAULT '{{}}'::jsonb,
    created         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated         TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted         TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS entries (
    id       TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL DEFAULT '{default_owner}',
    type     TEXT NOT NULL,
    source   TEXT NOT NULL,
    created  TIMESTAMPTZ NOT NULL,
    updated  TIMESTAMPTZ NOT NULL,
    expires  TIMESTAMPTZ,
    deleted  TIMESTAMPTZ,
    tags     JSONB NOT NULL DEFAULT '[]',
    data     JSONB NOT NULL DEFAULT '{{}}'::jsonb,
    logical_key TEXT
);
CREATE INDEX IF NOT EXISTS idx_entries_owner
    ON entries(owner_id);
CREATE INDEX IF NOT EXISTS idx_entries_owner_type
    ON entries(owner_id, type);
CREATE INDEX IF NOT EXISTS idx_entries_owner_source
    ON entries(owner_id, source);
CREATE INDEX IF NOT EXISTS idx_entries_owner_type_source
    ON entries(owner_id, type, source);
CREATE INDEX IF NOT EXISTS idx_entries_tags_gin
    ON entries USING GIN (tags);
CREATE UNIQUE INDEX IF NOT EXISTS idx_entries_source_logical_key
    ON entries(owner_id, source, logical_key)
    WHERE logical_key IS NOT NULL AND deleted IS NULL;

CREATE TABLE IF NOT EXISTS reads (
    id       SERIAL PRIMARY KEY,
    owner_id TEXT NOT NULL DEFAULT '{default_owner}',
    entry_id TEXT NOT NULL REFERENCES entries(id) ON DELETE CASCADE,
    timestamp TIMESTAMPTZ NOT NULL DEFAULT now(),
    platform TEXT,
    tool_used TEXT
);
CREATE INDEX IF NOT EXISTS idx_reads_owner ON reads(owner_id);
CREATE INDEX IF NOT EXISTS idx_reads_entry ON reads(entry_id);
CREATE INDEX IF NOT EXISTS idx_reads_timestamp ON reads(timestamp);

CREATE TABLE IF NOT EXISTS actions (
    id       SERIAL PRIMARY KEY,
    owner_id TEXT NOT NULL DEFAULT '{default_owner}',
    entry_id TEXT NOT NULL REFERENCES entries(id) ON DELETE CASCADE,
    timestamp TIMESTAMPTZ NOT NULL DEFAULT now(),
    platform TEXT,
    action   TEXT NOT NULL,
    detail   TEXT,
    tags     JSONB NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS idx_actions_owner ON actions(owner_id);
CREATE INDEX IF NOT EXISTS idx_actions_entry ON actions(entry_id);
CREATE INDEX IF NOT EXISTS idx_actions_timestamp ON actions(timestamp);
CREATE INDEX IF NOT EXISTS idx_actions_tags_gin ON actions USING GIN (tags);

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS embeddings (
    id          SERIAL PRIMARY KEY,
    owner_id    TEXT NOT NULL DEFAULT '{default_owner}',
    entry_id    TEXT NOT NULL REFERENCES entries(id) ON DELETE CASCADE,
    model       TEXT NOT NULL,
    dimensions  INTEGER NOT NULL,
    text_hash   TEXT NOT NULL,
    embedding   VECTOR({embedding_dimensions}) NOT NULL,
    created     TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (entry_id, model)
);
CREATE INDEX IF NOT EXISTS idx_embeddings_owner
    ON embeddings(owner_id);
CREATE INDEX IF NOT EXISTS idx_embeddings_entry
    ON embeddings(entry_id);
CREATE INDEX IF NOT EXISTS idx_embeddings_vector_hnsw
    ON embeddings USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);
