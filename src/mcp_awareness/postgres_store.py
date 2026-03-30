# mcp-awareness — ambient system awareness for AI agents
# Copyright (C) 2026 Chris Means
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

"""PostgreSQL storage backend for the awareness store.

Implements the Store protocol using psycopg (sync driver) with JSONB
for tags and data, GIN indexes for fast tag queries, and connection
pooling via psycopg_pool for concurrent request handling.

Requires: pip install psycopg[binary] psycopg_pool
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from .schema import Entry, EntryType, ensure_dt, ensure_dt_optional, make_id, now_utc, to_iso

# Default owner for backward compatibility — used as column DEFAULT in DDL
# so inserts without explicit owner_id still work.
_DEFAULT_OWNER = os.environ.get("AWARENESS_DEFAULT_OWNER", "system")

logger = logging.getLogger(__name__)

_SQL_DIR = Path(__file__).parent / "sql"
_sql_cache: dict[str, str] = {}


def _load_sql(name: str) -> str:
    """Load a SQL template from the sql/ directory, caching on first read."""
    if name not in _sql_cache:
        _sql_cache[name] = (_SQL_DIR / f"{name}.sql").read_text()
    return _sql_cache[name]


# How long soft-deleted entries remain recoverable before auto-purge
TRASH_RETENTION_DAYS = 30


class PostgresStore:
    def __init__(
        self,
        dsn: str,
        min_pool: int = 2,
        max_pool: int = 10,
        embedding_dimensions: int = 768,
    ) -> None:
        self.dsn = dsn
        self._embedding_dimensions = embedding_dimensions
        self._pool: ConnectionPool[psycopg.Connection[dict[str, Any]]] = ConnectionPool(
            dsn,
            min_size=min_pool,
            max_size=max_pool,
            kwargs={"row_factory": dict_row},
            open=True,
        )
        self._last_cleanup: float = 0.0
        self._cleanup_interval: float = 10.0
        self._cleanup_thread: threading.Thread | None = None
        self._create_tables()

    def _create_tables(self) -> None:
        from psycopg import sql

        ddl = sql.SQL(_load_sql("create_tables")).format(
            default_owner=sql.Literal(_DEFAULT_OWNER),
            embedding_dimensions=sql.SQL(str(self._embedding_dimensions)),
        )
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(ddl)
        # Note: schema migrations are managed by Alembic (see alembic/ directory).
        # Run `alembic upgrade head` before starting the server on a new database.

    # ------------------------------------------------------------------
    # RLS context helper
    # ------------------------------------------------------------------

    @staticmethod
    def _set_rls_context(cur: psycopg.Cursor[Any], owner_id: str) -> None:
        """Set the RLS context variable for the current transaction.

        Must be called inside a ``conn.transaction()`` block so that
        the setting scopes to the transaction and is automatically reset
        when the transaction ends. This keeps pool connections clean for
        the next user.

        Uses ``set_config()`` instead of ``SET LOCAL`` because SET does not
        support parameterized values (the identifier gets interpolated as-is).
        ``set_config(name, value, is_local)`` with ``true`` is equivalent to
        ``SET LOCAL``.
        """
        cur.execute("SELECT set_config('app.current_user', %s, true)", (owner_id,))

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
            updated=ensure_dt_optional(row["updated"]),
            expires=ensure_dt_optional(row["expires"]),
            data=data,
            logical_key=row.get("logical_key"),
        )

    def _insert_entry(self, cur: psycopg.Cursor[Any], owner_id: str, entry: Entry) -> None:
        cur.execute(
            _load_sql("insert_entry"),
            (
                entry.id,
                owner_id,
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
        """Run the actual DELETE using a pool connection (background thread).

        Uses SET LOCAL row_security = off because cleanup is a system-wide
        maintenance task — expired entries should be cleaned regardless of owner.
        """
        try:
            with self._pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
                cur.execute(_load_sql("disable_row_security"))
                now = datetime.now(timezone.utc)
                cur.execute(_load_sql("cleanup_expired"), (now,))
        except Exception as exc:
            print(f"[awareness] cleanup failed: {type(exc).__name__}: {exc}")

    def _query_entries(
        self,
        owner_id: str,
        where: str = "1=1",
        params: tuple[Any, ...] = (),
        order_by: str = "COALESCE(updated, created) DESC",
        limit: int | None = None,
        offset: int | None = None,
    ) -> list[Entry]:
        limit_clause = ""
        if limit is not None:
            limit_clause += " LIMIT %s"
        if offset is not None:
            limit_clause += " OFFSET %s"
        sql = _load_sql("query_entries").format(
            where=where, order_by=order_by, limit_clause=limit_clause
        )
        query_params: list[Any] = [owner_id, *params]
        if limit is not None:
            query_params.append(limit)
        if offset is not None:
            query_params.append(offset)
        with self._pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
            self._set_rls_context(cur, owner_id)
            cur.execute(sql, tuple(query_params))
            return [self._row_to_entry(r) for r in cur.fetchall()]

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def add(self, owner_id: str, entry: Entry) -> Entry:
        self._cleanup_expired()
        with self._pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
            self._set_rls_context(cur, owner_id)
            self._insert_entry(cur, owner_id, entry)
        return entry

    def upsert_status(
        self, owner_id: str, source: str, tags: list[str], data: dict[str, Any]
    ) -> Entry:
        """Upsert a status entry for a source (one active status per source)."""
        self._cleanup_expired()
        now = now_utc()
        with self._pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
            self._set_rls_context(cur, owner_id)
            cur.execute(
                _load_sql("upsert_status_delete"),
                (owner_id, EntryType.STATUS.value, source),
            )
            entry = Entry(
                id=make_id(),
                type=EntryType.STATUS,
                source=source,
                tags=tags,
                created=now,
                expires=None,
                data=data,
            )
            self._insert_entry(cur, owner_id, entry)
        return entry

    def upsert_alert(
        self, owner_id: str, source: str, tags: list[str], alert_id: str, data: dict[str, Any]
    ) -> Entry:
        """Upsert an alert by source + alert_id.

        Uses pg_advisory_xact_lock to serialize concurrent upserts for the same
        (owner_id, source, alert_id) key, then SELECT FOR UPDATE to safely lock
        any existing row before deciding whether to UPDATE or INSERT.
        """
        self._cleanup_expired()
        now = now_utc()
        # Derive a stable 64-bit advisory lock key from the logical upsert key.
        # hashlib gives a stable cross-process hash; mask to signed int64 range.
        raw = hashlib.sha256(f"{owner_id}:{source}:{alert_id}".encode()).digest()
        lock_key = int.from_bytes(raw[:8], "big") % (2**63)
        with self._pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
            self._set_rls_context(cur, owner_id)
            # Acquire advisory lock — serializes concurrent upserts for this key.
            cur.execute("SELECT pg_advisory_xact_lock(%s)", (lock_key,))
            cur.execute(
                _load_sql("select_alert_for_update"),
                (owner_id, EntryType.ALERT.value, source, alert_id),
            )
            row = cur.fetchone()
            if row:
                e = self._row_to_entry(row)
                e.updated = now
                e.tags = tags
                e.data.update(data)
                cur.execute(
                    _load_sql("upsert_alert_update"),
                    (now, json.dumps(e.tags), json.dumps(e.data), e.id, owner_id),
                )
                return e
            entry = Entry(
                id=make_id(),
                type=EntryType.ALERT,
                source=source,
                tags=tags,
                created=now,
                expires=None,
                data=data,
            )
            self._insert_entry(cur, owner_id, entry)
        return entry

    def upsert_preference(
        self, owner_id: str, key: str, scope: str, tags: list[str], data: dict[str, Any]
    ) -> Entry:
        """Upsert a preference by key + scope."""
        self._cleanup_expired()
        now = now_utc()
        with self._pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
            self._set_rls_context(cur, owner_id)
            # Advisory lock serializes concurrent upserts for the same key+scope
            lock_key = int(
                hashlib.sha256(f"pref:{owner_id}:{key}:{scope}".encode()).hexdigest(), 16
            ) % (2**63)
            cur.execute("SELECT pg_advisory_xact_lock(%s)", (lock_key,))
            cur.execute(
                _load_sql("select_preference_for_update"),
                (owner_id, EntryType.PREFERENCE.value, key, scope),
            )
            row = cur.fetchone()
            if row:
                e = self._row_to_entry(row)
                e.updated = now
                e.tags = tags
                e.data.update(data)
                cur.execute(
                    _load_sql("upsert_preference_update"),
                    (now, json.dumps(e.tags), json.dumps(e.data), e.id, owner_id),
                )
                return e
            entry = Entry(
                id=make_id(),
                type=EntryType.PREFERENCE,
                source=scope,
                tags=tags,
                created=now,
                expires=None,
                data=data,
            )
            self._insert_entry(cur, owner_id, entry)
        return entry

    def get_entries(
        self,
        owner_id: str,
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
            # AND logic: entry must contain ALL given tags
            for t in tags:
                clauses.append("tags @> %s::jsonb")
                params.append(json.dumps([t]))
        if since is not None:
            clauses.append("COALESCE(updated, created) >= %s")
            params.append(since)
        where = " AND ".join(clauses) if clauses else "1=1"
        return self._query_entries(owner_id, where, tuple(params), limit=limit, offset=offset)

    def get_sources(self, owner_id: str) -> list[str]:
        """Get all unique sources that have reported status."""
        with self._pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
            self._set_rls_context(cur, owner_id)
            cur.execute(
                _load_sql("get_sources"),
                (owner_id, EntryType.STATUS.value),
            )
            return [row["source"] for row in cur.fetchall()]

    def get_latest_status(self, owner_id: str, source: str) -> Entry | None:
        with self._pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
            self._set_rls_context(cur, owner_id)
            cur.execute(
                _load_sql("get_latest_status"),
                (owner_id, EntryType.STATUS.value, source),
            )
            row = cur.fetchone()
        return self._row_to_entry(row) if row else None

    def get_active_alerts(
        self,
        owner_id: str,
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
            clauses.append("COALESCE(updated, created) >= %s")
            params.append(since)
        where = " AND ".join(clauses)
        return self._query_entries(owner_id, where, tuple(params), limit=limit, offset=offset)

    def get_active_suppressions(self, owner_id: str, source: str | None = None) -> list[Entry]:
        clauses = ["type = %s", "(expires IS NULL OR expires > NOW())"]
        params: list[Any] = [EntryType.SUPPRESSION.value]
        if source:
            clauses.append("(source = %s OR source = '')")
            params.append(source)
        where = " AND ".join(clauses)
        return self._query_entries(owner_id, where, tuple(params))

    def get_patterns(self, owner_id: str, source: str | None = None) -> list[Entry]:
        if source:
            return self._query_entries(
                owner_id,
                "type = %s AND source = %s",
                (EntryType.PATTERN.value, source),
            )
        return self._query_entries(owner_id, "type = %s", (EntryType.PATTERN.value,))

    def count_active_suppressions(self, owner_id: str) -> int:
        with self._pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
            self._set_rls_context(cur, owner_id)
            cur.execute(
                _load_sql("count_active_suppressions"),
                (owner_id, EntryType.SUPPRESSION.value),
            )
            row = cur.fetchone()
        return row["cnt"] if row else 0

    def get_knowledge(
        self,
        owner_id: str,
        tags: list[str] | None = None,
        include_history: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        source: str | None = None,
        entry_type: EntryType | None = None,
        learned_from: str | None = None,
        created_after: datetime | None = None,
        created_before: datetime | None = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> list[Entry]:
        """Get knowledge entries (patterns, context, preferences, notes)."""
        if entry_type is not None:
            clauses = ["type = %s"]
            params: list[Any] = [entry_type.value]
        else:
            types = [
                EntryType.PATTERN.value,
                EntryType.CONTEXT.value,
                EntryType.PREFERENCE.value,
                EntryType.NOTE.value,
            ]
            placeholders = ",".join("%s" for _ in types)
            clauses = [f"type IN ({placeholders})"]
            params = list(types)
        if source is not None:
            clauses.append("source = %s")
            params.append(source)
        if tags:
            # AND logic: entry must contain ALL given tags
            for t in tags:
                clauses.append("tags @> %s::jsonb")
                params.append(json.dumps([t]))
        if since is not None:
            clauses.append("COALESCE(updated, created) >= %s")
            params.append(since)
        if until is not None:
            clauses.append("COALESCE(updated, created) <= %s")
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
        entries = self._query_entries(
            owner_id, where, tuple(params), limit=sql_limit, offset=sql_offset
        )
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

    def get_entry_by_id(self, owner_id: str, entry_id: str) -> Entry | None:
        """Get a single entry by ID (active only)."""
        results = self._query_entries(owner_id, "id = %s", (entry_id,))
        return results[0] if results else None

    def get_entries_by_ids(self, owner_id: str, entry_ids: list[str]) -> list[Entry]:
        """Get multiple entries by ID in a single query (active only)."""
        if not entry_ids:
            return []
        placeholders = ", ".join(["%s"] * len(entry_ids))
        return self._query_entries(owner_id, f"id IN ({placeholders})", tuple(entry_ids))

    def update_entry(self, owner_id: str, entry_id: str, updates: dict[str, Any]) -> Entry | None:
        """Update an entry in place, appending previous values to changelog."""
        entry = self.get_entry_by_id(owner_id, entry_id)
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

        with self._pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
            self._set_rls_context(cur, owner_id)
            cur.execute(
                _load_sql("update_entry"),
                (
                    now,
                    entry.source,
                    json.dumps(entry.tags),
                    json.dumps(entry.data),
                    entry.id,
                    owner_id,
                ),
            )
        return entry

    def upsert_by_logical_key(
        self, owner_id: str, source: str, logical_key: str, entry: Entry
    ) -> tuple[Entry, bool]:
        """Upsert by source + logical_key. Returns (entry, created).

        Uses a single connection with INSERT ... ON CONFLICT to avoid race
        conditions when concurrent writers target the same logical_key.
        """
        with self._pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
            self._set_rls_context(cur, owner_id)
            # Attempt insert; on conflict, fetch the existing row's id
            cur.execute(
                _load_sql("upsert_by_logical_key"),
                (
                    entry.id,
                    owner_id,
                    entry.type.value if isinstance(entry.type, EntryType) else entry.type,
                    entry.source,
                    entry.created,
                    entry.updated,
                    entry.expires,
                    json.dumps(entry.tags),
                    json.dumps(entry.data),
                    entry.logical_key,
                ),
            )
            row = cur.fetchone()
            assert row is not None
            inserted: bool = row["inserted"]

        if inserted:
            self._cleanup_expired()
            return (entry, True)

        # Existing entry — compute diff and update if needed
        existing = self._query_entries(
            owner_id, "source = %s AND logical_key = %s", (source, logical_key)
        )
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
            result = self.update_entry(owner_id, old.id, updates)
            return (result or old, False)
        return (old, False)

    def get_stats(self, owner_id: str) -> dict[str, Any]:
        """Get entry counts by type, list of sources, and total count."""
        with self._pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
            self._set_rls_context(cur, owner_id)
            cur.execute(_load_sql("get_stats_counts"), (owner_id,))
            counts = {row["type"]: row["cnt"] for row in cur.fetchall()}
            cur.execute(_load_sql("get_stats_sources"), (owner_id,))
            sources = [row["source"] for row in cur.fetchall()]
        return {
            "entries": {t.value: counts.get(t.value, 0) for t in EntryType},
            "sources": sources,
            "total": sum(counts.values()),
        }

    def get_tags(self, owner_id: str) -> list[dict[str, Any]]:
        """Get all tags in use with usage counts."""
        with self._pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
            self._set_rls_context(cur, owner_id)
            cur.execute(_load_sql("get_tags"), (owner_id,))
            return [{"tag": row["value"], "count": row["cnt"]} for row in cur.fetchall()]

    # ------------------------------------------------------------------
    # Soft delete / trash
    # ------------------------------------------------------------------

    def soft_delete_by_id(self, owner_id: str, entry_id: str) -> bool:
        """Soft-delete a single entry. Returns True if an entry was trashed."""
        now = datetime.now(timezone.utc)
        trash_expires = now + timedelta(days=TRASH_RETENTION_DAYS)
        with self._pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
            self._set_rls_context(cur, owner_id)
            cur.execute(
                _load_sql("soft_delete_by_id"),
                (now, trash_expires, owner_id, entry_id),
            )
            affected = cur.rowcount
        return affected > 0

    def soft_delete_by_tags(self, owner_id: str, tags: list[str]) -> int:
        """Soft-delete all entries matching ALL given tags (AND logic).

        Returns the number of trashed entries.
        """
        if not tags:
            return 0
        now = datetime.now(timezone.utc)
        trash_expires = now + timedelta(days=TRASH_RETENTION_DAYS)
        # AND: entry must contain every tag — use @> for each
        tag_clauses = " AND ".join("tags @> %s::jsonb" for _ in tags)
        params: list[Any] = [now, trash_expires, owner_id]
        params.extend(json.dumps([t]) for t in tags)
        with self._pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
            self._set_rls_context(cur, owner_id)
            cur.execute(
                _load_sql("soft_delete_by_tags").format(tag_clauses=tag_clauses),
                tuple(params),
            )
            affected = cur.rowcount
        return affected

    def soft_delete_by_source(
        self,
        owner_id: str,
        source: str,
        entry_type: EntryType | None = None,
    ) -> int:
        """Soft-delete all entries for a source, optionally filtered by type."""
        now = datetime.now(timezone.utc)
        trash_expires = now + timedelta(days=TRASH_RETENTION_DAYS)
        clauses = ["owner_id = %s", "source = %s", "deleted IS NULL"]
        params: list[Any] = [owner_id, source]
        if entry_type is not None:
            clauses.append("type = %s")
            params.append(entry_type.value)
        where = " AND ".join(clauses)
        with self._pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
            self._set_rls_context(cur, owner_id)
            cur.execute(
                _load_sql("soft_delete_by_source").format(where=where),
                (now, trash_expires, *params),
            )
            affected = cur.rowcount
        return affected

    def get_deleted(
        self,
        owner_id: str,
        since: datetime | None = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> list[Entry]:
        """Get all soft-deleted entries (the trash)."""
        clauses = ["owner_id = %s", "deleted IS NOT NULL"]
        params: list[Any] = [owner_id]
        if since is not None:
            clauses.append("deleted >= %s")
            params.append(since)
        where = " AND ".join(clauses)
        limit_clause = ""
        if limit is not None:
            limit_clause += " LIMIT %s"
            params.append(limit)
        if offset is not None:
            limit_clause += " OFFSET %s"
            params.append(offset)
        sql = _load_sql("get_deleted").format(where=where, limit_clause=limit_clause)
        with self._pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
            self._set_rls_context(cur, owner_id)
            cur.execute(sql, tuple(params))
            return [self._row_to_entry(r) for r in cur.fetchall()]

    def restore_by_id(self, owner_id: str, entry_id: str) -> bool:
        """Restore a soft-deleted entry. Returns True if restored.

        Recovers the original expires value that was saved during soft-delete.
        """
        with self._pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
            self._set_rls_context(cur, owner_id)
            cur.execute(
                _load_sql("restore_by_id"),
                (owner_id, entry_id),
            )
            affected = cur.rowcount
        return affected > 0

    def restore_by_tags(self, owner_id: str, tags: list[str]) -> int:
        """Restore all soft-deleted entries matching ALL given tags (AND logic).

        Returns the number of restored entries.
        """
        if not tags:
            return 0
        tag_clauses = " AND ".join("tags @> %s::jsonb" for _ in tags)
        params: list[Any] = [owner_id]
        params.extend(json.dumps([t]) for t in tags)
        with self._pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
            self._set_rls_context(cur, owner_id)
            cur.execute(
                _load_sql("restore_by_tags").format(tag_clauses=tag_clauses),
                tuple(params),
            )
            affected = cur.rowcount
        return affected

    # ------------------------------------------------------------------
    # Read / action tracking
    # ------------------------------------------------------------------

    def log_read(
        self, owner_id: str, entry_ids: list[str], tool_used: str, platform: str | None = None
    ) -> None:
        """Log that entries were read. Fire-and-forget — failures are silent."""
        if not entry_ids:
            return
        try:
            with self._pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
                self._set_rls_context(cur, owner_id)
                for eid in entry_ids:
                    cur.execute(
                        _load_sql("log_read"),
                        (owner_id, eid, platform, tool_used),
                    )
        except Exception:
            logger.debug("log_read failed", exc_info=True)

    def log_action(
        self,
        owner_id: str,
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
        entry = self.get_entry_by_id(owner_id, entry_id)
        if entry is None:
            return {"status": "error", "message": f"Entry not found: {entry_id}"}
        if tags is None:
            tags = entry.tags
        now = now_utc()
        with self._pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
            self._set_rls_context(cur, owner_id)
            cur.execute(
                _load_sql("log_action"),
                (owner_id, entry_id, now, platform, action, detail, json.dumps(tags)),
            )
            row = cur.fetchone()
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
        owner_id: str,
        entry_id: str | None = None,
        since: datetime | None = None,
        platform: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Get read history, optionally filtered."""
        clauses: list[str] = ["owner_id = %s"]
        params: list[Any] = [owner_id]
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
        limit_clause = ""
        if limit:
            limit_clause = f" LIMIT {int(limit)}"
        sql = _load_sql("get_reads").format(where=where, limit_clause=limit_clause)
        with self._pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
            self._set_rls_context(cur, owner_id)
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
        owner_id: str,
        entry_id: str | None = None,
        since: datetime | None = None,
        platform: str | None = None,
        tags: list[str] | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Get action history, optionally filtered."""
        clauses: list[str] = ["owner_id = %s"]
        params: list[Any] = [owner_id]
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
        limit_clause = ""
        if limit:
            limit_clause = f" LIMIT {int(limit)}"
        sql = _load_sql("get_actions").format(where=where, limit_clause=limit_clause)
        with self._pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
            self._set_rls_context(cur, owner_id)
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

    def get_unread(
        self, owner_id: str, since: datetime | None = None, limit: int | None = None
    ) -> list[Entry]:
        """Get entries with zero reads (optionally since a timestamp). Cleanup candidates."""
        since_clause = ""
        params: list[Any] = [owner_id]
        if since:
            since_clause = "AND r.timestamp >= %s"
            params.append(since)
        limit_clause = ""
        if limit is not None:
            limit_clause = " LIMIT %s"
            params.append(limit)
        sql = _load_sql("get_unread").format(since_clause=since_clause, limit_clause=limit_clause)
        with self._pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
            self._set_rls_context(cur, owner_id)
            cur.execute(sql, params)
            return [self._row_to_entry(r) for r in cur.fetchall()]

    def get_activity(
        self,
        owner_id: str,
        since: datetime | None = None,
        platform: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Get combined read + action activity feed, chronologically."""
        clauses_r: list[str] = ["owner_id = %s"]
        clauses_a: list[str] = ["owner_id = %s"]
        params_r: list[Any] = [owner_id]
        params_a: list[Any] = [owner_id]
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
        sql = _load_sql("get_activity").format(
            where_r=where_r, where_a=where_a, limit_clause=limit_clause
        )
        with self._pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
            self._set_rls_context(cur, owner_id)
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

    def get_read_counts(self, owner_id: str, entry_ids: list[str]) -> dict[str, dict[str, Any]]:
        """Get read_count and last_read for a list of entry IDs. For list mode enrichment."""
        if not entry_ids:
            return {}
        placeholders = ",".join("%s" for _ in entry_ids)
        sql = _load_sql("get_read_counts").format(placeholders=placeholders)
        with self._pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
            self._set_rls_context(cur, owner_id)
            cur.execute(sql, (owner_id, *entry_ids))
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
        owner_id: str,
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
        return self._query_entries(owner_id, where, tuple(params), limit=limit)

    def update_intention_state(
        self, owner_id: str, entry_id: str, new_state: str, reason: str | None = None
    ) -> Entry | None:
        """Transition an intention to a new state. Appends to changelog."""
        entry = self.get_entry_by_id(owner_id, entry_id)
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
        with self._pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
            self._set_rls_context(cur, owner_id)
            cur.execute(
                _load_sql("update_intention_state"),
                (now, json.dumps(entry.data), entry.id, owner_id),
            )
        return entry

    def get_fired_intentions(self, owner_id: str) -> list[Entry]:
        """Get intentions whose deliver_at has passed and state is still pending.

        These are ready to be surfaced to the user. The caller (collator or
        server tool) is responsible for transitioning them to 'fired'.
        """
        now = now_utc()
        entries = self._query_entries(
            owner_id,
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
        owner_id: str,
        entry_id: str,
        model: str,
        dimensions: int,
        text_hash: str,
        embedding: list[float],
    ) -> None:
        """Store or update an embedding for an entry + model pair."""
        vector_literal = "[" + ",".join(str(v) for v in embedding) + "]"
        with self._pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
            self._set_rls_context(cur, owner_id)
            cur.execute(
                _load_sql("upsert_embedding"),
                (owner_id, entry_id, model, dimensions, text_hash, vector_literal),
            )

    def get_entries_without_embeddings(
        self,
        owner_id: str,
        model: str,
        limit: int = 100,
    ) -> list[Entry]:
        """Find active entries that have no embedding for the given model.

        Excludes suppression entries (short-lived, not worth embedding).
        """
        with self._pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
            self._set_rls_context(cur, owner_id)
            cur.execute(
                _load_sql("get_entries_without_embeddings"),
                (model, owner_id, EntryType.SUPPRESSION.value, limit),
            )
            return [self._row_to_entry(r) for r in cur.fetchall()]

    def get_stale_embeddings(
        self,
        owner_id: str,
        model: str,
        limit: int = 100,
    ) -> list[Entry]:
        """Find entries whose embedding text_hash differs from their current content.

        Returns entries that have an embedding but whose text has changed since
        it was generated. The caller should re-embed these entries.
        """
        from .embeddings import compose_embedding_text as _compose
        from .embeddings import text_hash as _hash

        with self._pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
            self._set_rls_context(cur, owner_id)
            cur.execute(
                _load_sql("get_stale_embeddings"),
                (model, owner_id, limit),
            )
            rows = cur.fetchall()
        stale: list[Entry] = []
        for r in rows:
            entry = self._row_to_entry(r)
            current_hash = _hash(_compose(entry))
            if current_hash != r["emb_text_hash"]:
                stale.append(entry)
        return stale

    def semantic_search(
        self,
        owner_id: str,
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
        clauses = ["e.owner_id = %s", "e.deleted IS NULL"]
        params: list[Any] = [model, vector_literal, owner_id]
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
            clauses.append("COALESCE(e.updated, e.created) >= %s")
            params.append(since)
        if until is not None:
            clauses.append("COALESCE(e.updated, e.created) <= %s")
            params.append(until)
        where = " AND ".join(clauses)
        params.append(limit)
        sql = _load_sql("semantic_search").format(where=where)
        # query_vector (similarity), model, ...filters, query_vector (ORDER BY), limit
        ordered_params = [vector_literal, model, *params[2:-1], vector_literal, limit]
        with self._pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
            self._set_rls_context(cur, owner_id)
            cur.execute(sql, tuple(ordered_params))
            rows = cur.fetchall()
        return [(self._row_to_entry(r), float(r["similarity"])) for r in rows]

    def get_referencing_entries(self, owner_id: str, entry_id: str) -> list[Entry]:
        """Find active entries whose data.related_ids contains the given entry_id."""
        return self._query_entries(
            owner_id,
            "data->'related_ids' @> %s::jsonb",
            (json.dumps([entry_id]),),
        )

    # ------------------------------------------------------------------
    # User operations (for OAuth auto-provisioning)
    # ------------------------------------------------------------------

    def get_user(self, user_id: str) -> dict[str, Any] | None:
        """Look up a user by ID. Returns dict or None if not found."""
        with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(_load_sql("get_user"), (user_id,))
            return cur.fetchone()

    def create_user_if_not_exists(
        self,
        user_id: str,
        email: str | None = None,
        display_name: str | None = None,
        oauth_subject: str | None = None,
        oauth_issuer: str | None = None,
    ) -> None:
        """Auto-provision a user on first OAuth login. No-op if user exists."""
        from .helpers import canonical_email

        canon = canonical_email(email) if email else None
        with self._pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
            cur.execute(
                _load_sql("create_user_auto"),
                (user_id, email, canon, display_name, oauth_subject, oauth_issuer),
            )

    def get_user_by_oauth(self, oauth_issuer: str, oauth_subject: str) -> dict[str, Any] | None:
        """Look up a user by OAuth identity. Returns dict or None."""
        with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(_load_sql("get_user_by_oauth"), (oauth_issuer, oauth_subject))
            return cur.fetchone()

    def link_oauth_identity(self, oauth_subject: str, oauth_issuer: str, email: str) -> str | None:
        """Link an OAuth identity to a pre-provisioned user matched by canonical email.

        Returns the user ID if linked, None if no matching user found.
        Only links if the user's oauth_subject is currently NULL (first-time link).
        Uses canonical_email for matching (handles Gmail dot/+tag variants).
        """
        from .helpers import canonical_email

        canon = canonical_email(email)
        with (
            self._pool.connection() as conn,
            conn.transaction(),
            conn.cursor(row_factory=dict_row) as cur,
        ):
            cur.execute(_load_sql("link_oauth_identity"), (oauth_subject, oauth_issuer, canon))
            row = cur.fetchone()
            return str(row["id"]) if row else None

    def clear(self, owner_id: str) -> None:
        with self._pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
            self._set_rls_context(cur, owner_id)
            cur.execute(_load_sql("clear_reads"), (owner_id,))
            cur.execute(_load_sql("clear_actions"), (owner_id,))
            cur.execute(_load_sql("clear_embeddings"), (owner_id,))
            cur.execute(_load_sql("clear_entries"), (owner_id,))
