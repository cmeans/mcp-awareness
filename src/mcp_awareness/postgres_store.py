"""PostgreSQL storage backend for the awareness store.

Implements the Store protocol using psycopg (sync driver) with JSONB
for tags and data, GIN indexes for fast tag queries, and native
concurrency (no application-level locking).

Requires: pip install psycopg[binary]
"""

from __future__ import annotations

import contextlib
import json
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import psycopg
from psycopg.rows import dict_row

from .schema import Entry, EntryType, ensure_dt, ensure_dt_optional, make_id, now_utc, to_iso

# How long soft-deleted entries remain recoverable before auto-purge
TRASH_RETENTION_DAYS = 30


class PostgresStore:
    # How often to check connection health (seconds)
    _HEALTH_CHECK_INTERVAL = 30.0

    def __init__(self, dsn: str) -> None:
        self.dsn = dsn
        self.__conn: psycopg.Connection[dict[str, Any]] = self._new_conn()
        self._last_health_check: float = time.monotonic()
        self._last_cleanup: float = 0.0
        self._cleanup_interval: float = 10.0
        self._cleanup_thread: threading.Thread | None = None
        self._create_tables()

    def _new_conn(self) -> psycopg.Connection[dict[str, Any]]:
        """Create a new database connection."""
        return psycopg.connect(self.dsn, row_factory=dict_row, autocommit=False)

    @property
    def _conn(self) -> psycopg.Connection[dict[str, Any]]:
        """Auto-healing connection property.

        Checks connection health at most every _HEALTH_CHECK_INTERVAL seconds.
        If the connection is closed or broken, reconnects transparently.
        All existing code using self._conn benefits automatically.
        """
        now = time.monotonic()
        if now - self._last_health_check < self._HEALTH_CHECK_INTERVAL:
            return self.__conn
        self._last_health_check = now
        try:
            if self.__conn.closed:
                self.__conn = self._new_conn()
            else:
                self.__conn.execute("SELECT 1")
                self.__conn.rollback()
        except (psycopg.OperationalError, psycopg.InterfaceError):
            with contextlib.suppress(Exception):
                self.__conn.close()
            self.__conn = self._new_conn()
        return self.__conn

    def _create_tables(self) -> None:
        with self._conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS entries (
                    id       TEXT PRIMARY KEY,
                    type     TEXT NOT NULL,
                    source   TEXT NOT NULL,
                    created  TIMESTAMPTZ NOT NULL,
                    updated  TIMESTAMPTZ NOT NULL,
                    expires  TIMESTAMPTZ,
                    deleted  TIMESTAMPTZ,
                    tags     JSONB NOT NULL DEFAULT '[]',
                    data     JSONB NOT NULL DEFAULT '{}',
                    logical_key TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_entries_type ON entries(type);
                CREATE INDEX IF NOT EXISTS idx_entries_source ON entries(source);
                CREATE INDEX IF NOT EXISTS idx_entries_type_source ON entries(type, source);
                CREATE INDEX IF NOT EXISTS idx_entries_tags_gin ON entries USING GIN (tags);

                CREATE TABLE IF NOT EXISTS reads (
                    id       SERIAL PRIMARY KEY,
                    entry_id TEXT NOT NULL REFERENCES entries(id) ON DELETE CASCADE,
                    timestamp TIMESTAMPTZ NOT NULL DEFAULT now(),
                    platform TEXT,
                    tool_used TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_reads_entry ON reads(entry_id);
                CREATE INDEX IF NOT EXISTS idx_reads_timestamp ON reads(timestamp);

                CREATE TABLE IF NOT EXISTS actions (
                    id       SERIAL PRIMARY KEY,
                    entry_id TEXT NOT NULL REFERENCES entries(id) ON DELETE CASCADE,
                    timestamp TIMESTAMPTZ NOT NULL DEFAULT now(),
                    platform TEXT,
                    action   TEXT NOT NULL,
                    detail   TEXT,
                    tags     JSONB NOT NULL DEFAULT '[]'
                );
                CREATE INDEX IF NOT EXISTS idx_actions_entry ON actions(entry_id);
                CREATE INDEX IF NOT EXISTS idx_actions_timestamp ON actions(timestamp);
                CREATE INDEX IF NOT EXISTS idx_actions_tags_gin ON actions USING GIN (tags);

                CREATE EXTENSION IF NOT EXISTS vector;

                CREATE TABLE IF NOT EXISTS embeddings (
                    id          SERIAL PRIMARY KEY,
                    entry_id    TEXT NOT NULL REFERENCES entries(id) ON DELETE CASCADE,
                    model       TEXT NOT NULL,
                    dimensions  INTEGER NOT NULL,
                    text_hash   TEXT NOT NULL,
                    embedding   VECTOR(768) NOT NULL,
                    created     TIMESTAMPTZ NOT NULL DEFAULT now(),
                    UNIQUE (entry_id, model)
                );
                CREATE INDEX IF NOT EXISTS idx_embeddings_entry
                    ON embeddings(entry_id);
                CREATE INDEX IF NOT EXISTS idx_embeddings_vector_hnsw
                    ON embeddings USING hnsw (embedding vector_cosine_ops)
                    WITH (m = 16, ef_construction = 64);
            """)
        self._conn.commit()
        # Note: schema migrations are managed by Alembic (see alembic/ directory).
        # Run `alembic upgrade head` before starting the server on a new database.

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _row_to_entry(row: dict[str, Any]) -> Entry:
        tags = row["tags"]
        data = row["data"]
        # psycopg returns JSONB as Python objects already
        if isinstance(tags, str):
            tags = json.loads(tags)
        if isinstance(data, str):
            data = json.loads(data)
        return Entry(
            id=row["id"],
            type=EntryType(row["type"]),
            source=row["source"],
            tags=tags,
            created=ensure_dt(row["created"]),
            updated=ensure_dt(row["updated"]),
            expires=ensure_dt_optional(row["expires"]),
            data=data,
            logical_key=row.get("logical_key"),
        )

    def _insert_entry(self, cur: psycopg.Cursor[Any], entry: Entry) -> None:
        cur.execute(
            """INSERT INTO entries
               (id, type, source, created, updated, expires, tags, data, logical_key)
               VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s)""",
            (
                entry.id,
                entry.type.value if isinstance(entry.type, EntryType) else entry.type,
                entry.source,
                entry.created,  # datetime → TIMESTAMPTZ natively
                entry.updated,
                entry.expires,
                json.dumps(entry.tags),
                json.dumps(entry.data),
                entry.logical_key,
            ),
        )

    def _cleanup_expired(self) -> None:
        """Schedule cleanup of expired entries on a background thread (debounced).

        Guards against thread accumulation: skips if a previous cleanup thread
        is still running.
        """
        now = time.monotonic()
        if now - self._last_cleanup < self._cleanup_interval:
            return
        if self._cleanup_thread is not None and self._cleanup_thread.is_alive():
            return
        self._last_cleanup = now
        self._cleanup_thread = threading.Thread(
            target=self._do_cleanup, name="awareness-pg-cleanup", daemon=True
        )
        self._cleanup_thread.start()

    def _do_cleanup(self) -> None:
        """Run the actual DELETE on a dedicated connection (background thread)."""
        try:
            with psycopg.connect(self.dsn) as conn:
                now = datetime.now(timezone.utc)
                conn.execute(
                    "DELETE FROM entries WHERE expires IS NOT NULL AND expires <= %s",
                    (now,),
                )
                conn.commit()
        except Exception as exc:
            print(f"[awareness] cleanup failed: {type(exc).__name__}: {exc}")

    _ACTIVE = "deleted IS NULL"

    def _query_entries(
        self,
        where: str = "1=1",
        params: tuple[Any, ...] = (),
        order_by: str = "updated DESC",
        limit: int | None = None,
        offset: int | None = None,
    ) -> list[Entry]:
        sql = f"SELECT * FROM entries WHERE {self._ACTIVE} AND ({where}) ORDER BY {order_by}"
        query_params = list(params)
        if limit is not None:
            sql += " LIMIT %s"
            query_params.append(limit)
        if offset is not None:
            sql += " OFFSET %s"
            query_params.append(offset)
        with self._conn.cursor() as cur:
            cur.execute(sql, tuple(query_params))
            return [self._row_to_entry(r) for r in cur.fetchall()]

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def add(self, entry: Entry) -> Entry:
        self._cleanup_expired()
        with self._conn.cursor() as cur:
            self._insert_entry(cur, entry)
        self._conn.commit()
        return entry

    def upsert_status(self, source: str, tags: list[str], data: dict[str, Any]) -> Entry:
        """Upsert a status entry for a source (one active status per source)."""
        self._cleanup_expired()
        now = now_utc()
        with self._conn.cursor() as cur:
            cur.execute(
                f"DELETE FROM entries WHERE type = %s AND source = %s AND {self._ACTIVE}",
                (EntryType.STATUS.value, source),
            )
            entry = Entry(
                id=make_id(),
                type=EntryType.STATUS,
                source=source,
                tags=tags,
                created=now,
                updated=now,
                expires=None,
                data=data,
            )
            self._insert_entry(cur, entry)
        self._conn.commit()
        return entry

    def upsert_alert(
        self, source: str, tags: list[str], alert_id: str, data: dict[str, Any]
    ) -> Entry:
        """Upsert an alert by source + alert_id."""
        self._cleanup_expired()
        now = now_utc()
        existing = self._query_entries(
            "type = %s AND source = %s AND data->>'alert_id' = %s",
            (EntryType.ALERT.value, source, alert_id),
        )
        if existing:
            e = existing[0]
            e.updated = now
            e.tags = tags
            e.data.update(data)
            with self._conn.cursor() as cur:
                cur.execute(
                    "UPDATE entries SET updated = %s, tags = %s::jsonb, "
                    "data = %s::jsonb WHERE id = %s",
                    (now, json.dumps(e.tags), json.dumps(e.data), e.id),
                )
            self._conn.commit()
            return e
        entry = Entry(
            id=make_id(),
            type=EntryType.ALERT,
            source=source,
            tags=tags,
            created=now,
            updated=now,
            expires=None,
            data=data,
        )
        with self._conn.cursor() as cur:
            self._insert_entry(cur, entry)
        self._conn.commit()
        return entry

    def upsert_preference(
        self, key: str, scope: str, tags: list[str], data: dict[str, Any]
    ) -> Entry:
        """Upsert a preference by key + scope."""
        self._cleanup_expired()
        now = now_utc()
        existing = self._query_entries(
            "type = %s AND data->>'key' = %s AND data->>'scope' = %s",
            (EntryType.PREFERENCE.value, key, scope),
        )
        if existing:
            e = existing[0]
            e.updated = now
            e.tags = tags
            e.data.update(data)
            with self._conn.cursor() as cur:
                cur.execute(
                    "UPDATE entries SET updated = %s, tags = %s::jsonb, "
                    "data = %s::jsonb WHERE id = %s",
                    (now, json.dumps(e.tags), json.dumps(e.data), e.id),
                )
            self._conn.commit()
            return e
        entry = Entry(
            id=make_id(),
            type=EntryType.PREFERENCE,
            source=scope,
            tags=tags,
            created=now,
            updated=now,
            expires=None,
            data=data,
        )
        with self._conn.cursor() as cur:
            self._insert_entry(cur, entry)
        self._conn.commit()
        return entry

    def get_entries(
        self,
        entry_type: EntryType | None = None,
        source: str | None = None,
        tags: list[str] | None = None,
        since: datetime | None = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> list[Entry]:
        clauses: list[str] = []
        params: list[Any] = []
        if entry_type is not None:
            clauses.append("type = %s")
            params.append(entry_type.value)
        if source is not None:
            clauses.append("source = %s")
            params.append(source)
        if tags:
            # Use GIN-indexed @> operator: match entries containing ANY of the tags
            tag_clauses = ["tags @> %s::jsonb" for _ in tags]
            clauses.append(f"({' OR '.join(tag_clauses)})")
            params.extend(json.dumps([t]) for t in tags)
        if since is not None:
            clauses.append("updated >= %s")
            params.append(since)
        where = " AND ".join(clauses) if clauses else "1=1"
        return self._query_entries(where, tuple(params), limit=limit, offset=offset)

    def get_sources(self) -> list[str]:
        """Get all unique sources that have reported status."""
        with self._conn.cursor() as cur:
            cur.execute(
                f"SELECT DISTINCT source FROM entries WHERE type = %s AND {self._ACTIVE}",
                (EntryType.STATUS.value,),
            )
            return [row["source"] for row in cur.fetchall()]

    def get_latest_status(self, source: str) -> Entry | None:
        with self._conn.cursor() as cur:
            cur.execute(
                f"SELECT * FROM entries WHERE type = %s AND source = %s AND {self._ACTIVE}"
                " ORDER BY created DESC LIMIT 1",
                (EntryType.STATUS.value, source),
            )
            row = cur.fetchone()
        return self._row_to_entry(row) if row else None

    def get_active_alerts(
        self,
        source: str | None = None,
        since: datetime | None = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> list[Entry]:
        clauses = [
            "type = %s",
            "NOT (data @> '{\"resolved\": true}'::jsonb)",
        ]
        params: list[Any] = [EntryType.ALERT.value]
        if source:
            clauses.append("source = %s")
            params.append(source)
        if since is not None:
            clauses.append("updated >= %s")
            params.append(since)
        where = " AND ".join(clauses)
        return self._query_entries(where, tuple(params), limit=limit, offset=offset)

    def get_active_suppressions(self, source: str | None = None) -> list[Entry]:
        clauses = ["type = %s", "(expires IS NULL OR expires > NOW())"]
        params: list[Any] = [EntryType.SUPPRESSION.value]
        if source:
            clauses.append("(source = %s OR source = '')")
            params.append(source)
        where = " AND ".join(clauses)
        return self._query_entries(where, tuple(params))

    def get_patterns(self, source: str | None = None) -> list[Entry]:
        if source:
            return self._query_entries(
                "type = %s AND source = %s",
                (EntryType.PATTERN.value, source),
            )
        return self._query_entries("type = %s", (EntryType.PATTERN.value,))

    def count_active_suppressions(self) -> int:
        with self._conn.cursor() as cur:
            cur.execute(
                f"SELECT COUNT(*) AS cnt FROM entries WHERE type = %s AND {self._ACTIVE}",
                (EntryType.SUPPRESSION.value,),
            )
            row = cur.fetchone()
        return row["cnt"] if row else 0

    def get_knowledge(
        self,
        tags: list[str] | None = None,
        include_history: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        source: str | None = None,
        learned_from: str | None = None,
        created_after: datetime | None = None,
        created_before: datetime | None = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> list[Entry]:
        """Get knowledge entries (patterns, context, preferences, notes)."""
        types = [
            EntryType.PATTERN.value,
            EntryType.CONTEXT.value,
            EntryType.PREFERENCE.value,
            EntryType.NOTE.value,
        ]
        placeholders = ",".join("%s" for _ in types)
        clauses = [f"type IN ({placeholders})"]
        params: list[Any] = list(types)
        if source is not None:
            clauses.append("source = %s")
            params.append(source)
        if tags:
            tag_clauses = ["tags @> %s::jsonb" for _ in tags]
            clauses.append(f"({' OR '.join(tag_clauses)})")
            params.extend(json.dumps([t]) for t in tags)
        if since is not None:
            clauses.append("updated >= %s")
            params.append(since)
        if until is not None:
            clauses.append("updated <= %s")
            params.append(until)
        if learned_from is not None:
            clauses.append("data->>'learned_from' = %s")
            params.append(learned_from)
        if created_after is not None:
            clauses.append("created >= %s")
            params.append(created_after)
        if created_before is not None:
            clauses.append("created <= %s")
            params.append(created_before)
        where = " AND ".join(clauses)
        # Push LIMIT/OFFSET to SQL unless include_history="only" (post-filter changes count)
        sql_limit = limit if include_history != "only" else None
        sql_offset = offset if include_history != "only" else None
        entries = self._query_entries(where, tuple(params), limit=sql_limit, offset=sql_offset)
        if include_history == "only":
            entries = [e for e in entries if e.data.get("changelog")]
            if offset:
                entries = entries[offset:]
            if limit:
                entries = entries[:limit]
        elif include_history != "true":
            for e in entries:
                e.data.pop("changelog", None)
        return entries

    def get_entry_by_id(self, entry_id: str) -> Entry | None:
        """Get a single entry by ID (active only)."""
        results = self._query_entries("id = %s", (entry_id,))
        return results[0] if results else None

    def update_entry(self, entry_id: str, updates: dict[str, Any]) -> Entry | None:
        """Update an entry in place, appending previous values to changelog."""
        entry = self.get_entry_by_id(entry_id)
        if entry is None:
            return None
        immutable = {EntryType.STATUS, EntryType.ALERT, EntryType.SUPPRESSION}
        if entry.type in immutable:
            return None

        self._cleanup_expired()
        now = now_utc()
        changed: dict[str, Any] = {}
        for field in ("source", "tags"):
            if field in updates and updates[field] != getattr(entry, field):
                changed[field] = getattr(entry, field)
                setattr(entry, field, updates[field])
        for field in ("description", "content", "content_type"):
            if field in updates and updates[field] != entry.data.get(field):
                old_val = entry.data.get(field)
                if old_val is not None:
                    changed[field] = old_val
                entry.data[field] = updates[field]

        if not changed:
            return entry

        changelog = entry.data.setdefault("changelog", [])
        changelog.append({"updated": to_iso(now), "changed": changed})
        entry.updated = now

        with self._conn.cursor() as cur:
            cur.execute(
                "UPDATE entries SET updated = %s, source = %s, "
                "tags = %s::jsonb, data = %s::jsonb WHERE id = %s",
                (now, entry.source, json.dumps(entry.tags), json.dumps(entry.data), entry.id),
            )
        self._conn.commit()
        return entry

    def upsert_by_logical_key(
        self, source: str, logical_key: str, entry: Entry
    ) -> tuple[Entry, bool]:
        """Upsert by source + logical_key. Returns (entry, created)."""
        existing = self._query_entries("source = %s AND logical_key = %s", (source, logical_key))
        if existing:
            old = existing[0]
            updates: dict[str, Any] = {}
            if entry.tags != old.tags:
                updates["tags"] = entry.tags
            for field in ("description", "content", "content_type"):
                new_val = entry.data.get(field)
                old_val = old.data.get(field)
                if new_val is not None and new_val != old_val:
                    updates[field] = new_val
            if updates:
                result = self.update_entry(old.id, updates)
                return (result or old, False)
            return (old, False)
        self._cleanup_expired()
        with self._conn.cursor() as cur:
            self._insert_entry(cur, entry)
        self._conn.commit()
        return (entry, True)

    def get_stats(self) -> dict[str, Any]:
        """Get entry counts by type, list of sources, and total count."""
        with self._conn.cursor() as cur:
            cur.execute(
                f"SELECT type, COUNT(*) AS cnt FROM entries WHERE {self._ACTIVE} GROUP BY type"
            )
            counts = {row["type"]: row["cnt"] for row in cur.fetchall()}
            cur.execute(f"SELECT DISTINCT source FROM entries WHERE {self._ACTIVE} ORDER BY source")
            sources = [row["source"] for row in cur.fetchall()]
        return {
            "entries": {t.value: counts.get(t.value, 0) for t in EntryType},
            "sources": sources,
            "total": sum(counts.values()),
        }

    def get_tags(self) -> list[dict[str, Any]]:
        """Get all tags in use with usage counts."""
        with self._conn.cursor() as cur:
            cur.execute(
                f"SELECT value, COUNT(*) AS cnt FROM entries, "
                f"jsonb_array_elements_text(tags) AS value "
                f"WHERE {self._ACTIVE} GROUP BY value ORDER BY cnt DESC"
            )
            return [{"tag": row["value"], "count": row["cnt"]} for row in cur.fetchall()]

    # ------------------------------------------------------------------
    # Soft delete / trash
    # ------------------------------------------------------------------

    def soft_delete_by_id(self, entry_id: str) -> bool:
        """Soft-delete a single entry. Returns True if an entry was trashed."""
        now = datetime.now(timezone.utc)
        expires = now + timedelta(days=TRASH_RETENTION_DAYS)
        with self._conn.cursor() as cur:
            cur.execute(
                f"UPDATE entries SET deleted = %s, expires = %s WHERE id = %s AND {self._ACTIVE}",
                (now, expires, entry_id),
            )
            affected = cur.rowcount
        self._conn.commit()
        return affected > 0

    def soft_delete_by_tags(self, tags: list[str]) -> int:
        """Soft-delete all entries matching ALL given tags (AND logic).

        Returns the number of trashed entries.
        """
        if not tags:
            return 0
        now = datetime.now(timezone.utc)
        expires = now + timedelta(days=TRASH_RETENTION_DAYS)
        # AND: entry must contain every tag — use @> for each
        tag_clauses = " AND ".join("tags @> %s::jsonb" for _ in tags)
        params: list[Any] = [now, expires]
        params.extend(json.dumps([t]) for t in tags)
        with self._conn.cursor() as cur:
            cur.execute(
                f"UPDATE entries SET deleted = %s, expires = %s "
                f"WHERE {self._ACTIVE} AND {tag_clauses}",
                tuple(params),
            )
            affected = cur.rowcount
        self._conn.commit()
        return affected

    def soft_delete_by_source(
        self,
        source: str,
        entry_type: EntryType | None = None,
    ) -> int:
        """Soft-delete all entries for a source, optionally filtered by type."""
        now = datetime.now(timezone.utc)
        expires = now + timedelta(days=TRASH_RETENTION_DAYS)
        clauses = ["source = %s", self._ACTIVE]
        params: list[Any] = [source]
        if entry_type is not None:
            clauses.append("type = %s")
            params.append(entry_type.value)
        where = " AND ".join(clauses)
        with self._conn.cursor() as cur:
            cur.execute(
                f"UPDATE entries SET deleted = %s, expires = %s WHERE {where}",
                (now, expires, *params),
            )
            affected = cur.rowcount
        self._conn.commit()
        return affected

    def get_deleted(
        self, since: datetime | None = None, limit: int | None = None, offset: int | None = None
    ) -> list[Entry]:
        """Get all soft-deleted entries (the trash)."""
        clauses = ["deleted IS NOT NULL"]
        params: list[Any] = []
        if since is not None:
            clauses.append("deleted >= %s")
            params.append(since)
        where = " AND ".join(clauses)
        sql = f"SELECT * FROM entries WHERE {where} ORDER BY deleted DESC"
        if limit is not None:
            sql += " LIMIT %s"
            params.append(limit)
        if offset is not None:
            sql += " OFFSET %s"
            params.append(offset)
        with self._conn.cursor() as cur:
            cur.execute(sql, tuple(params))
            return [self._row_to_entry(r) for r in cur.fetchall()]

    def restore_by_id(self, entry_id: str) -> bool:
        """Restore a soft-deleted entry. Returns True if restored."""
        with self._conn.cursor() as cur:
            cur.execute(
                "UPDATE entries SET deleted = NULL, expires = NULL"
                " WHERE id = %s AND deleted IS NOT NULL",
                (entry_id,),
            )
            affected = cur.rowcount
        self._conn.commit()
        return affected > 0

    def restore_by_tags(self, tags: list[str]) -> int:
        """Restore all soft-deleted entries matching ALL given tags (AND logic).

        Returns the number of restored entries.
        """
        if not tags:
            return 0
        tag_clauses = " AND ".join("tags @> %s::jsonb" for _ in tags)
        params = [json.dumps([t]) for t in tags]
        with self._conn.cursor() as cur:
            cur.execute(
                "UPDATE entries SET deleted = NULL, expires = NULL "
                f"WHERE deleted IS NOT NULL AND {tag_clauses}",
                tuple(params),
            )
            affected = cur.rowcount
        self._conn.commit()
        return affected

    # ------------------------------------------------------------------
    # Read / action tracking
    # ------------------------------------------------------------------

    def log_read(self, entry_ids: list[str], tool_used: str, platform: str | None = None) -> None:
        """Log that entries were read. Fire-and-forget — failures are silent."""
        if not entry_ids:
            return
        try:
            with self._conn.cursor() as cur:
                for eid in entry_ids:
                    cur.execute(
                        "INSERT INTO reads (entry_id, platform, tool_used) VALUES (%s, %s, %s)",
                        (eid, platform, tool_used),
                    )
            self._conn.commit()
        except Exception:
            # Fire-and-forget: read logging never blocks a response
            import contextlib

            with contextlib.suppress(Exception):
                self._conn.rollback()

    def log_action(
        self,
        entry_id: str,
        action: str,
        platform: str | None = None,
        detail: str | None = None,
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        """Log an action taken because of an entry. Returns the action record.

        Returns {"status": "error", ...} if the entry doesn't exist.
        """
        # Validate entry exists and copy tags if not provided
        entry = self.get_entry_by_id(entry_id)
        if entry is None:
            return {"status": "error", "message": f"Entry not found: {entry_id}"}
        if tags is None:
            tags = entry.tags
        now = now_utc()
        with self._conn.cursor() as cur:
            cur.execute(
                "INSERT INTO actions (entry_id, timestamp, platform, action, detail, tags) "
                "VALUES (%s, %s, %s, %s, %s, %s::jsonb) RETURNING id",
                (entry_id, now, platform, action, detail, json.dumps(tags)),
            )
            row = cur.fetchone()
        self._conn.commit()
        return {
            "id": row["id"] if row else None,
            "entry_id": entry_id,
            "timestamp": to_iso(now),
            "platform": platform,
            "action": action,
            "detail": detail,
            "tags": tags,
        }

    def get_reads(
        self,
        entry_id: str | None = None,
        since: datetime | None = None,
        platform: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Get read history, optionally filtered."""
        clauses: list[str] = []
        params: list[Any] = []
        if entry_id:
            clauses.append("entry_id = %s")
            params.append(entry_id)
        if since:
            clauses.append("timestamp >= %s")
            params.append(since)
        if platform:
            clauses.append("platform = %s")
            params.append(platform)
        where = " AND ".join(clauses) if clauses else "1=1"
        sql = f"SELECT * FROM reads WHERE {where} ORDER BY timestamp DESC"
        if limit:
            sql += f" LIMIT {int(limit)}"
        with self._conn.cursor() as cur:
            cur.execute(sql, tuple(params))
            rows = cur.fetchall()
        return [
            {
                "id": r["id"],
                "entry_id": r["entry_id"],
                "timestamp": to_iso(r["timestamp"]),
                "platform": r["platform"],
                "tool_used": r["tool_used"],
            }
            for r in rows
        ]

    def get_actions(
        self,
        entry_id: str | None = None,
        since: datetime | None = None,
        platform: str | None = None,
        tags: list[str] | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Get action history, optionally filtered."""
        clauses: list[str] = []
        params: list[Any] = []
        if entry_id:
            clauses.append("entry_id = %s")
            params.append(entry_id)
        if since:
            clauses.append("timestamp >= %s")
            params.append(since)
        if platform:
            clauses.append("platform = %s")
            params.append(platform)
        if tags:
            for t in tags:
                clauses.append("tags @> %s::jsonb")
                params.append(json.dumps([t]))
        where = " AND ".join(clauses) if clauses else "1=1"
        sql = f"SELECT * FROM actions WHERE {where} ORDER BY timestamp DESC"
        if limit:
            sql += f" LIMIT {int(limit)}"
        with self._conn.cursor() as cur:
            cur.execute(sql, tuple(params))
            rows = cur.fetchall()
        return [
            {
                "id": r["id"],
                "entry_id": r["entry_id"],
                "timestamp": to_iso(r["timestamp"]),
                "platform": r["platform"],
                "action": r["action"],
                "detail": r["detail"],
                "tags": r["tags"] if isinstance(r["tags"], list) else json.loads(r["tags"]),
            }
            for r in rows
        ]

    def get_unread(self, since: datetime | None = None) -> list[Entry]:
        """Get entries with zero reads (optionally since a timestamp). Cleanup candidates."""
        since_clause = ""
        params: tuple[Any, ...] = ()
        if since:
            since_clause = "AND r.timestamp >= %s"
            params = (since,)
        with self._conn.cursor() as cur:
            cur.execute(
                f"SELECT e.* FROM entries e "
                f"LEFT JOIN reads r ON e.id = r.entry_id {since_clause} "
                f"WHERE e.deleted IS NULL AND r.id IS NULL "
                f"ORDER BY e.created DESC",
                params,
            )
            return [self._row_to_entry(r) for r in cur.fetchall()]

    def get_activity(
        self,
        since: datetime | None = None,
        platform: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Get combined read + action activity feed, chronologically."""
        clauses_r: list[str] = []
        clauses_a: list[str] = []
        params_r: list[Any] = []
        params_a: list[Any] = []
        if since:
            clauses_r.append("timestamp >= %s")
            clauses_a.append("timestamp >= %s")
            params_r.append(since)
            params_a.append(since)
        if platform:
            clauses_r.append("platform = %s")
            clauses_a.append("platform = %s")
            params_r.append(platform)
            params_a.append(platform)
        where_r = " AND ".join(clauses_r) if clauses_r else "1=1"
        where_a = " AND ".join(clauses_a) if clauses_a else "1=1"
        limit_clause = f"LIMIT {int(limit)}" if limit else ""
        sql = (
            f"SELECT 'read' AS event_type, entry_id, timestamp, platform, "
            f"tool_used AS detail, NULL AS action, '[]'::jsonb AS tags FROM reads WHERE {where_r} "
            f"UNION ALL "
            f"SELECT 'action' AS event_type, entry_id, timestamp, platform, "
            f"detail, action, tags FROM actions WHERE {where_a} "
            f"ORDER BY timestamp DESC {limit_clause}"
        )
        with self._conn.cursor() as cur:
            cur.execute(sql, tuple(params_r + params_a))
            rows = cur.fetchall()
        return [
            {
                "event_type": r["event_type"],
                "entry_id": r["entry_id"],
                "timestamp": to_iso(r["timestamp"]),
                "platform": r["platform"],
                "action": r["action"],
                "detail": r["detail"],
                "tags": r["tags"] if isinstance(r["tags"], list) else json.loads(r["tags"]),
            }
            for r in rows
        ]

    def get_read_counts(self, entry_ids: list[str]) -> dict[str, dict[str, Any]]:
        """Get read_count and last_read for a list of entry IDs. For list mode enrichment."""
        if not entry_ids:
            return {}
        placeholders = ",".join("%s" for _ in entry_ids)
        with self._conn.cursor() as cur:
            cur.execute(
                f"SELECT entry_id, COUNT(*) AS cnt, MAX(timestamp) AS last "
                f"FROM reads WHERE entry_id IN ({placeholders}) GROUP BY entry_id",
                tuple(entry_ids),
            )
            rows = cur.fetchall()
        result: dict[str, dict[str, Any]] = {}
        for r in rows:
            result[r["entry_id"]] = {
                "read_count": r["cnt"],
                "last_read": to_iso(r["last"]) if r["last"] else None,
            }
        return result

    # ------------------------------------------------------------------
    # Intentions
    # ------------------------------------------------------------------

    def get_intentions(
        self,
        state: str | None = None,
        source: str | None = None,
        tags: list[str] | None = None,
        limit: int | None = None,
    ) -> list[Entry]:
        """Get intention entries, optionally filtered by state, source, or tags."""
        clauses = ["type = %s"]
        params: list[Any] = [EntryType.INTENTION.value]
        if state:
            clauses.append("data->>'state' = %s")
            params.append(state)
        if source:
            clauses.append("source = %s")
            params.append(source)
        if tags:
            for t in tags:
                clauses.append("tags @> %s::jsonb")
                params.append(json.dumps([t]))
        where = " AND ".join(clauses)
        return self._query_entries(where, tuple(params), limit=limit)

    def update_intention_state(
        self, entry_id: str, new_state: str, reason: str | None = None
    ) -> Entry | None:
        """Transition an intention to a new state. Appends to changelog."""
        entry = self.get_entry_by_id(entry_id)
        if entry is None or entry.type != EntryType.INTENTION:
            return None
        old_state = entry.data.get("state", "pending")
        old_reason = entry.data.get("state_reason")
        now = now_utc()
        # Update state
        entry.data["state"] = new_state
        if reason:
            entry.data["state_reason"] = reason
        # Changelog — capture previous values
        changelog = entry.data.setdefault("changelog", [])
        changed: dict[str, Any] = {"state": old_state}
        if old_reason is not None:
            changed["state_reason"] = old_reason
        changelog.append({"updated": to_iso(now), "changed": changed})
        entry.updated = now
        with self._conn.cursor() as cur:
            cur.execute(
                "UPDATE entries SET updated = %s, data = %s::jsonb WHERE id = %s",
                (now, json.dumps(entry.data), entry.id),
            )
        self._conn.commit()
        return entry

    def get_fired_intentions(self) -> list[Entry]:
        """Get intentions whose deliver_at has passed and state is still pending.

        These are ready to be surfaced to the user. The caller (collator or
        server tool) is responsible for transitioning them to 'fired'.
        """
        now = now_utc()
        entries = self._query_entries(
            "type = %s AND data->>'state' = %s",
            (EntryType.INTENTION.value, "pending"),
        )
        return [
            e
            for e in entries
            if e.data.get("deliver_at") and ensure_dt(e.data["deliver_at"]) <= now
        ]

    # ------------------------------------------------------------------
    # Embeddings / semantic search
    # ------------------------------------------------------------------

    def upsert_embedding(
        self,
        entry_id: str,
        model: str,
        dimensions: int,
        text_hash: str,
        embedding: list[float],
    ) -> None:
        """Store or update an embedding for an entry + model pair."""
        vector_literal = "[" + ",".join(str(v) for v in embedding) + "]"
        with self._conn.cursor() as cur:
            cur.execute(
                "INSERT INTO embeddings (entry_id, model, dimensions, text_hash, embedding) "
                "VALUES (%s, %s, %s, %s, %s::vector) "
                "ON CONFLICT (entry_id, model) DO UPDATE SET "
                "embedding = EXCLUDED.embedding, text_hash = EXCLUDED.text_hash, "
                "dimensions = EXCLUDED.dimensions, created = now()",
                (entry_id, model, dimensions, text_hash, vector_literal),
            )
        self._conn.commit()

    def get_entries_without_embeddings(
        self,
        model: str,
        limit: int = 100,
    ) -> list[Entry]:
        """Find active entries that have no embedding for the given model.

        Excludes suppression entries (short-lived, not worth embedding).
        """
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT e.* FROM entries e "
                "LEFT JOIN embeddings emb ON e.id = emb.entry_id AND emb.model = %s "
                "WHERE e.deleted IS NULL AND emb.id IS NULL "
                "AND e.type != %s "
                "ORDER BY e.updated DESC LIMIT %s",
                (model, EntryType.SUPPRESSION.value, limit),
            )
            return [self._row_to_entry(r) for r in cur.fetchall()]

    def semantic_search(
        self,
        embedding: list[float],
        model: str,
        entry_type: EntryType | None = None,
        source: str | None = None,
        tags: list[str] | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 10,
    ) -> list[tuple[Entry, float]]:
        """Search entries by vector similarity, with optional filters.

        Returns (entry, similarity_score) pairs sorted by relevance.
        Similarity is 1 - cosine_distance (higher = more similar).
        """
        vector_literal = "[" + ",".join(str(v) for v in embedding) + "]"
        clauses = ["e.deleted IS NULL"]
        params: list[Any] = [model, vector_literal]
        if entry_type is not None:
            clauses.append("e.type = %s")
            params.append(entry_type.value)
        if source is not None:
            clauses.append("e.source = %s")
            params.append(source)
        if tags:
            for t in tags:
                clauses.append("e.tags @> %s::jsonb")
                params.append(json.dumps([t]))
        if since is not None:
            clauses.append("e.updated >= %s")
            params.append(since)
        if until is not None:
            clauses.append("e.updated <= %s")
            params.append(until)
        where = " AND ".join(clauses)
        params.append(limit)
        sql = (
            f"SELECT e.*, 1 - (emb.embedding <=> %s::vector) AS similarity "
            f"FROM entries e "
            f"JOIN embeddings emb ON e.id = emb.entry_id AND emb.model = %s "
            f"WHERE {where} "
            f"ORDER BY emb.embedding <=> %s::vector "
            f"LIMIT %s"
        )
        # query_vector (similarity), model, ...filters, query_vector (ORDER BY), limit
        ordered_params = [vector_literal, model, *params[2:-1], vector_literal, limit]
        with self._conn.cursor() as cur:
            cur.execute(sql, tuple(ordered_params))
            rows = cur.fetchall()
        return [(self._row_to_entry(r), float(r["similarity"])) for r in rows]

    def clear(self) -> None:
        with self._conn.cursor() as cur:
            cur.execute("DELETE FROM reads")
            cur.execute("DELETE FROM actions")
            cur.execute("DELETE FROM embeddings")
            cur.execute("DELETE FROM entries")
        self._conn.commit()
