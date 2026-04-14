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
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import psycopg
from psycopg import sql as psql
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from .language import SIMPLE
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
        self._valid_regconfigs: set[str] = set()
        self._create_tables()
        self._load_regconfigs()

    def _create_tables(self) -> None:
        ddl = psql.SQL(_load_sql("create_tables")).format(
            default_owner=psql.Literal(_DEFAULT_OWNER),
            embedding_dimensions=psql.SQL(str(self._embedding_dimensions)),
        )
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(ddl)
        # Note: schema migrations are managed by Alembic (see alembic/ directory).
        # Run `alembic upgrade head` before starting the server on a new database.

    def _load_regconfigs(self) -> None:
        """Cache valid Postgres regconfig names from pg_ts_config."""
        try:
            with self._pool.connection() as conn, conn.cursor() as cur:
                cur.execute("SELECT cfgname FROM pg_ts_config")
                self._valid_regconfigs = {row["cfgname"] for row in cur.fetchall()}
            logger.debug("Loaded %d regconfigs from pg_ts_config", len(self._valid_regconfigs))
        except Exception:
            logger.warning("Failed to load regconfigs from pg_ts_config", exc_info=True)
            self._valid_regconfigs = {SIMPLE}

    def validate_regconfig(self, regconfig: str) -> str:
        """Validate a regconfig name against the cached pg_ts_config set.

        Returns the regconfig if valid, or ``'simple'`` if not. On a cache
        miss, reloads from pg_ts_config once (an extension may have been
        installed after startup) before falling back.
        """
        if regconfig in self._valid_regconfigs:
            return regconfig
        # Reload once in case an extension was installed after startup
        self._load_regconfigs()
        if regconfig in self._valid_regconfigs:
            return regconfig
        logger.warning("Regconfig %r not in pg_ts_config, falling back to %r", regconfig, SIMPLE)
        return SIMPLE

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
            language=row.get("language", SIMPLE),
        )

    def _insert_entry(self, cur: psycopg.Cursor[Any], owner_id: str, entry: Entry) -> None:
        entry.language = self.validate_regconfig(entry.language)
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
                entry.language,
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
        """Delete expired entries for owners who opted in to auto-cleanup.

        Only processes owners with an ``auto_cleanup=true`` preference.
        RLS-safe — each DELETE is scoped by owner_id, no row_security
        bypass needed.  Owners who haven't opted in keep all their data.
        """
        try:
            now = datetime.now(timezone.utc)
            with self._pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
                cur.execute(_load_sql("get_cleanup_opted_in_owners"))
                owners = [row["owner_id"] for row in cur.fetchall()]
            for owner_id in owners:
                with self._pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
                    self._set_rls_context(cur, owner_id)
                    cur.execute(_load_sql("cleanup_expired"), (now, owner_id))
                    deleted = cur.rowcount
                    if deleted:
                        logger.info("Cleanup: removed %d expired entries for %s", deleted, owner_id)
        except Exception as exc:
            logger.error("Cleanup failed: %s: %s", type(exc).__name__, exc)

    _DEFAULT_WHERE: psql.SQL = psql.SQL("1=1")
    _DEFAULT_ORDER: psql.SQL = psql.SQL("COALESCE(updated, created) DESC")

    def _query_entries(
        self,
        owner_id: str,
        where: psql.Composable | None = None,
        params: tuple[Any, ...] = (),
        order_by: psql.Composable | None = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> list[Entry]:
        if where is None:
            where = self._DEFAULT_WHERE
        if order_by is None:
            order_by = self._DEFAULT_ORDER
        limit_clause: psql.Composable = psql.SQL("")
        if limit is not None:
            limit_clause += psql.SQL(" LIMIT %s")
        if offset is not None:
            limit_clause += psql.SQL(" OFFSET %s")
        query = psql.SQL(_load_sql("query_entries")).format(
            where=where, order_by=order_by, limit_clause=limit_clause
        )
        query_params: list[Any] = [owner_id, *params]
        if limit is not None:
            query_params.append(limit)
        if offset is not None:
            query_params.append(offset)
        with self._pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
            self._set_rls_context(cur, owner_id)
            cur.execute(query, tuple(query_params))
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
        lock_key = int(
            hashlib.sha256(f"alert:{owner_id}:{source}:{alert_id}".encode()).hexdigest(), 16
        ) % (2**63)
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
        """Upsert a preference by key + scope.

        Uses pg_advisory_xact_lock to serialize concurrent upserts for the same
        (owner_id, key, scope) key, then SELECT FOR UPDATE to safely lock
        any existing row before deciding whether to UPDATE or INSERT.
        """
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
        clauses: list[psql.SQL] = []
        params: list[Any] = []
        if entry_type is not None:
            clauses.append(psql.SQL("type = %s"))
            params.append(entry_type.value)
        if source is not None:
            clauses.append(psql.SQL("source = %s"))
            params.append(source)
        if tags:
            for t in tags:
                clauses.append(psql.SQL("tags @> %s::jsonb"))
                params.append(json.dumps([t]))
        if since is not None:
            clauses.append(psql.SQL("COALESCE(updated, created) >= %s"))
            params.append(since)
        where = psql.SQL(" AND ").join(clauses) if clauses else psql.SQL("1=1")
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
        clauses: list[psql.SQL] = [
            psql.SQL("type = %s"),
            psql.SQL("NOT (data @> '{\"resolved\": true}'::jsonb)"),
            psql.SQL("(expires IS NULL OR expires > NOW())"),
        ]
        params: list[Any] = [EntryType.ALERT.value]
        if source:
            clauses.append(psql.SQL("source = %s"))
            params.append(source)
        if since is not None:
            clauses.append(psql.SQL("COALESCE(updated, created) >= %s"))
            params.append(since)
        where = psql.SQL(" AND ").join(clauses)
        return self._query_entries(owner_id, where, tuple(params), limit=limit, offset=offset)

    def get_active_suppressions(self, owner_id: str, source: str | None = None) -> list[Entry]:
        clauses: list[psql.SQL] = [
            psql.SQL("type = %s"),
            psql.SQL("(expires IS NULL OR expires > NOW())"),
        ]
        params: list[Any] = [EntryType.SUPPRESSION.value]
        if source:
            clauses.append(psql.SQL("(source = %s OR source = '')"))
            params.append(source)
        where = psql.SQL(" AND ").join(clauses)
        return self._query_entries(owner_id, where, tuple(params))

    def get_patterns(self, owner_id: str, source: str | None = None) -> list[Entry]:
        if source:
            return self._query_entries(
                owner_id,
                psql.SQL("type = %s AND source = %s"),
                (EntryType.PATTERN.value, source),
            )
        return self._query_entries(owner_id, psql.SQL("type = %s"), (EntryType.PATTERN.value,))

    def get_all_statuses(self, owner_id: str) -> dict[str, Entry]:
        with self._pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
            self._set_rls_context(cur, owner_id)
            cur.execute(
                _load_sql("get_all_statuses"),
                (owner_id, EntryType.STATUS.value),
            )
            return {row["source"]: self._row_to_entry(row) for row in cur.fetchall()}

    def get_all_active_alerts(self, owner_id: str) -> dict[str, list[Entry]]:
        with self._pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
            self._set_rls_context(cur, owner_id)
            cur.execute(
                _load_sql("get_all_active_alerts"),
                (owner_id, EntryType.ALERT.value),
            )
            result: dict[str, list[Entry]] = defaultdict(list)
            for row in cur.fetchall():
                result[row["source"]].append(self._row_to_entry(row))
            return dict(result)

    def get_all_active_suppressions(self, owner_id: str) -> dict[str, list[Entry]]:
        with self._pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
            self._set_rls_context(cur, owner_id)
            cur.execute(
                _load_sql("get_all_active_suppressions"),
                (owner_id, EntryType.SUPPRESSION.value),
            )
            result: dict[str, list[Entry]] = defaultdict(list)
            for row in cur.fetchall():
                result[row["source"]].append(self._row_to_entry(row))
            return dict(result)

    def get_all_patterns(self, owner_id: str) -> dict[str, list[Entry]]:
        with self._pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
            self._set_rls_context(cur, owner_id)
            cur.execute(
                _load_sql("get_all_patterns"),
                (owner_id, EntryType.PATTERN.value),
            )
            result: dict[str, list[Entry]] = defaultdict(list)
            for row in cur.fetchall():
                result[row["source"]].append(self._row_to_entry(row))
            return dict(result)

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
        language: str | None = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> list[Entry]:
        """Get knowledge entries (patterns, context, preferences, notes)."""
        if entry_type is not None:
            clauses: list[psql.Composable] = [psql.SQL("type = %s")]
            params: list[Any] = [entry_type.value]
        else:
            types = [
                EntryType.PATTERN.value,
                EntryType.CONTEXT.value,
                EntryType.PREFERENCE.value,
                EntryType.NOTE.value,
            ]
            placeholders = psql.SQL(",").join(psql.SQL("%s") for _ in types)
            clauses = [psql.SQL("type IN ({})").format(placeholders)]
            params = list(types)
        if source is not None:
            clauses.append(psql.SQL("source = %s"))
            params.append(source)
        if tags:
            for t in tags:
                clauses.append(psql.SQL("tags @> %s::jsonb"))
                params.append(json.dumps([t]))
        if since is not None:
            clauses.append(psql.SQL("COALESCE(updated, created) >= %s"))
            params.append(since)
        if until is not None:
            clauses.append(psql.SQL("COALESCE(updated, created) <= %s"))
            params.append(until)
        if learned_from is not None:
            clauses.append(psql.SQL("data->>'learned_from' = %s"))
            params.append(learned_from)
        if created_after is not None:
            clauses.append(psql.SQL("created >= %s"))
            params.append(created_after)
        if created_before is not None:
            clauses.append(psql.SQL("created <= %s"))
            params.append(created_before)
        if language is not None:
            clauses.append(psql.SQL("language = %s::regconfig"))
            params.append(language)
        where = psql.SQL(" AND ").join(clauses)
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
        results = self._query_entries(owner_id, psql.SQL("id = %s"), (entry_id,))
        return results[0] if results else None

    def get_entries_by_ids(self, owner_id: str, entry_ids: list[str]) -> list[Entry]:
        """Get multiple entries by ID in a single query (active only)."""
        if not entry_ids:
            return []
        placeholders = psql.SQL(", ").join(psql.SQL("%s") for _ in entry_ids)
        return self._query_entries(
            owner_id, psql.SQL("id IN ({})").format(placeholders), tuple(entry_ids)
        )

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
        if "language" in updates and updates["language"] != entry.language:
            changed["language"] = entry.language
            entry.language = self.validate_regconfig(updates["language"])
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
                    entry.language,
                    entry.id,
                    owner_id,
                ),
            )
        return entry

    def upsert_by_logical_key(
        self, owner_id: str, source: str, logical_key: str, entry: Entry
    ) -> tuple[Entry, bool]:
        """Upsert by source + logical_key. Returns (entry, created).

        Uses a single connection for the entire operation: INSERT attempt,
        existing-row fetch, and conditional update all share one connection
        and transaction to avoid pool contention under concurrency.
        """
        entry.language = self.validate_regconfig(entry.language)
        with self._pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
            self._set_rls_context(cur, owner_id)
            # Attempt insert; on conflict, return inserted=false
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
                    entry.language,
                ),
            )
            row = cur.fetchone()
            assert row is not None
            inserted: bool = row["inserted"]

            if inserted:
                self._cleanup_expired()
                return (entry, True)

            # Existing entry — fetch within the same connection
            query_sql = psql.SQL(_load_sql("query_entries")).format(
                where=psql.SQL("source = %s AND logical_key = %s"),
                order_by=psql.SQL("COALESCE(updated, created) DESC"),
                limit_clause=psql.SQL(""),
            )
            cur.execute(query_sql, (owner_id, source, logical_key))
            rows = cur.fetchall()
            old = self._row_to_entry(rows[0])

            # Compute diff
            updates: dict[str, Any] = {}
            if entry.tags != old.tags:
                updates["tags"] = entry.tags
            for field in ("description", "content", "content_type"):
                new_val = entry.data.get(field)
                old_val = old.data.get(field)
                if new_val is not None and new_val != old_val:
                    updates[field] = new_val

            if not updates:
                return (old, False)

            # Apply updates inline (mirrors update_entry logic for knowledge types)
            now = now_utc()
            changed: dict[str, Any] = {}
            if "tags" in updates and updates["tags"] != old.tags:
                changed["tags"] = old.tags
                old.tags = updates["tags"]
            for field in ("description", "content", "content_type"):
                if field in updates and updates[field] != old.data.get(field):
                    old_val = old.data.get(field)
                    if old_val is not None:
                        changed[field] = old_val
                    old.data[field] = updates[field]

            if changed:
                changelog = old.data.setdefault("changelog", [])
                changelog.append({"updated": to_iso(now), "changed": changed})
                old.updated = now
                cur.execute(
                    _load_sql("update_entry"),
                    (
                        now,
                        old.source,
                        json.dumps(old.tags),
                        json.dumps(old.data),
                        old.language,
                        old.id,
                        owner_id,
                    ),
                )

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
        tag_clauses = psql.SQL(" AND ").join(psql.SQL("tags @> %s::jsonb") for _ in tags)
        params: list[Any] = [now, trash_expires, owner_id]
        params.extend(json.dumps([t]) for t in tags)
        with self._pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
            self._set_rls_context(cur, owner_id)
            cur.execute(
                psql.SQL(_load_sql("soft_delete_by_tags")).format(tag_clauses=tag_clauses),
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
        clauses: list[psql.SQL] = [
            psql.SQL("owner_id = %s"),
            psql.SQL("source = %s"),
            psql.SQL("deleted IS NULL"),
        ]
        params: list[Any] = [owner_id, source]
        if entry_type is not None:
            clauses.append(psql.SQL("type = %s"))
            params.append(entry_type.value)
        where = psql.SQL(" AND ").join(clauses)
        with self._pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
            self._set_rls_context(cur, owner_id)
            cur.execute(
                psql.SQL(_load_sql("soft_delete_by_source")).format(where=where),
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
        clauses: list[psql.SQL] = [psql.SQL("owner_id = %s"), psql.SQL("deleted IS NOT NULL")]
        params: list[Any] = [owner_id]
        if since is not None:
            clauses.append(psql.SQL("deleted >= %s"))
            params.append(since)
        where = psql.SQL(" AND ").join(clauses)
        limit_clause: psql.Composable = psql.SQL("")
        if limit is not None:
            limit_clause += psql.SQL(" LIMIT %s")
            params.append(limit)
        if offset is not None:
            limit_clause += psql.SQL(" OFFSET %s")
            params.append(offset)
        query = psql.SQL(_load_sql("get_deleted")).format(where=where, limit_clause=limit_clause)
        with self._pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
            self._set_rls_context(cur, owner_id)
            cur.execute(query, tuple(params))
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
        tag_clauses = psql.SQL(" AND ").join(psql.SQL("tags @> %s::jsonb") for _ in tags)
        params: list[Any] = [owner_id]
        params.extend(json.dumps([t]) for t in tags)
        with self._pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
            self._set_rls_context(cur, owner_id)
            cur.execute(
                psql.SQL(_load_sql("restore_by_tags")).format(tag_clauses=tag_clauses),
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
        clauses: list[psql.SQL] = [psql.SQL("owner_id = %s")]
        params: list[Any] = [owner_id]
        if entry_id:
            clauses.append(psql.SQL("entry_id = %s"))
            params.append(entry_id)
        if since:
            clauses.append(psql.SQL("timestamp >= %s"))
            params.append(since)
        if platform:
            clauses.append(psql.SQL("platform = %s"))
            params.append(platform)
        where = psql.SQL(" AND ").join(clauses) if clauses else psql.SQL("1=1")
        limit_clause = psql.SQL("")
        if limit:
            limit_clause = psql.SQL(" LIMIT %s")
            params.append(int(limit))
        query = psql.SQL(_load_sql("get_reads")).format(where=where, limit_clause=limit_clause)
        with self._pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
            self._set_rls_context(cur, owner_id)
            cur.execute(query, tuple(params))
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
        clauses: list[psql.SQL] = [psql.SQL("owner_id = %s")]
        params: list[Any] = [owner_id]
        if entry_id:
            clauses.append(psql.SQL("entry_id = %s"))
            params.append(entry_id)
        if since:
            clauses.append(psql.SQL("timestamp >= %s"))
            params.append(since)
        if platform:
            clauses.append(psql.SQL("platform = %s"))
            params.append(platform)
        if tags:
            for t in tags:
                clauses.append(psql.SQL("tags @> %s::jsonb"))
                params.append(json.dumps([t]))
        where = psql.SQL(" AND ").join(clauses) if clauses else psql.SQL("1=1")
        limit_clause = psql.SQL("")
        if limit:
            limit_clause = psql.SQL(" LIMIT %s")
            params.append(int(limit))
        query = psql.SQL(_load_sql("get_actions")).format(where=where, limit_clause=limit_clause)
        with self._pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
            self._set_rls_context(cur, owner_id)
            cur.execute(query, tuple(params))
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
        since_clause = psql.SQL("")
        # since_clause appears before owner_id in SQL, so since param must come first
        params: list[Any] = []
        if since:
            since_clause = psql.SQL("AND r.timestamp >= %s")
            params.append(since)
        params.append(owner_id)
        limit_clause = psql.SQL("")
        if limit is not None:
            limit_clause = psql.SQL(" LIMIT %s")
            params.append(limit)
        query = psql.SQL(_load_sql("get_unread")).format(
            since_clause=since_clause, limit_clause=limit_clause
        )
        with self._pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
            self._set_rls_context(cur, owner_id)
            cur.execute(query, params)
            return [self._row_to_entry(r) for r in cur.fetchall()]

    def get_activity(
        self,
        owner_id: str,
        since: datetime | None = None,
        platform: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Get combined read + action activity feed, chronologically."""
        clauses_r: list[psql.SQL] = [psql.SQL("owner_id = %s")]
        clauses_a: list[psql.SQL] = [psql.SQL("owner_id = %s")]
        params_r: list[Any] = [owner_id]
        params_a: list[Any] = [owner_id]
        if since:
            clauses_r.append(psql.SQL("timestamp >= %s"))
            clauses_a.append(psql.SQL("timestamp >= %s"))
            params_r.append(since)
            params_a.append(since)
        if platform:
            clauses_r.append(psql.SQL("platform = %s"))
            clauses_a.append(psql.SQL("platform = %s"))
            params_r.append(platform)
            params_a.append(platform)
        where_r = psql.SQL(" AND ").join(clauses_r) if clauses_r else psql.SQL("1=1")
        where_a = psql.SQL(" AND ").join(clauses_a) if clauses_a else psql.SQL("1=1")
        limit_clause = psql.SQL("")
        all_params = params_r + params_a
        if limit:
            limit_clause = psql.SQL("LIMIT %s")
            all_params.append(int(limit))
        query = psql.SQL(_load_sql("get_activity")).format(
            where_r=where_r, where_a=where_a, limit_clause=limit_clause
        )
        with self._pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
            self._set_rls_context(cur, owner_id)
            cur.execute(query, tuple(all_params))
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
        placeholders = psql.SQL(",").join(psql.SQL("%s") for _ in entry_ids)
        query = psql.SQL(_load_sql("get_read_counts")).format(placeholders=placeholders)
        with self._pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
            self._set_rls_context(cur, owner_id)
            cur.execute(query, (owner_id, *entry_ids))
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
        clauses: list[psql.SQL] = [psql.SQL("type = %s")]
        params: list[Any] = [EntryType.INTENTION.value]
        if state:
            clauses.append(psql.SQL("data->>'state' = %s"))
            params.append(state)
        if source:
            clauses.append(psql.SQL("source = %s"))
            params.append(source)
        if tags:
            for t in tags:
                clauses.append(psql.SQL("tags @> %s::jsonb"))
                params.append(json.dumps([t]))
        where = psql.SQL(" AND ").join(clauses)
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
        return self._query_entries(
            owner_id,
            psql.SQL(
                "type = %s AND data->>'state' = %s"
                " AND data->>'deliver_at' IS NOT NULL"
                " AND (data->>'deliver_at')::timestamptz <= %s"
            ),
            (EntryType.INTENTION.value, "pending", now),
        )

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
        query_text: str = "",
        query_language: str = SIMPLE,
        entry_type: EntryType | None = None,
        source: str | None = None,
        tags: list[str] | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 10,
    ) -> list[tuple[Entry, float]]:
        """Hybrid search: vector + FTS fused via Reciprocal Rank Fusion.

        Returns (entry, rrf_score) pairs sorted by fused relevance.
        Either branch may return zero rows — the CTE degrades gracefully.
        """
        vector_literal = "[" + ",".join(str(v) for v in embedding) + "]"

        # Build WHERE clauses shared by both CTE branches
        clauses: list[psql.SQL] = [
            psql.SQL("e.owner_id = %s"),
            psql.SQL("e.deleted IS NULL"),
        ]
        filter_params: list[Any] = [owner_id]
        if entry_type is not None:
            clauses.append(psql.SQL("e.type = %s"))
            filter_params.append(entry_type.value)
        if source is not None:
            clauses.append(psql.SQL("e.source = %s"))
            filter_params.append(source)
        if tags:
            for t in tags:
                clauses.append(psql.SQL("e.tags @> %s::jsonb"))
                filter_params.append(json.dumps([t]))
        if since is not None:
            clauses.append(psql.SQL("COALESCE(e.updated, e.created) >= %s"))
            filter_params.append(since)
        if until is not None:
            clauses.append(psql.SQL("COALESCE(e.updated, e.created) <= %s"))
            filter_params.append(until)
        where = psql.SQL(" AND ").join(clauses)

        query = psql.SQL(_load_sql("semantic_search")).format(where=where)

        # Positional params match the SQL template after {where} expansion:
        #   1: query_vector (vector_hits ROW_NUMBER window)
        #   2: model (embeddings JOIN)
        #   [filter_params for vector_hits WHERE]
        #   3: query_vector (vector_hits ORDER BY — after WHERE)
        #   4: query_language (plainto_tsquery regconfig)
        #   5: query_text (plainto_tsquery input)
        #   [filter_params for lexical_hits WHERE — duplicated]
        #   6: limit
        ordered_params: list[Any] = [
            vector_literal,
            model,
            *filter_params,
            vector_literal,
            query_language,
            query_text,
            *filter_params,
            limit,
        ]

        with self._pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
            self._set_rls_context(cur, owner_id)
            cur.execute(query, tuple(ordered_params))
            rows = cur.fetchall()
        return [(self._row_to_entry(r), float(r["similarity"])) for r in rows]

    def get_referencing_entries(self, owner_id: str, entry_id: str) -> list[Entry]:
        """Find active entries whose data.related_ids contains the given entry_id."""
        return self._query_entries(
            owner_id,
            psql.SQL("data->'related_ids' @> %s::jsonb"),
            (json.dumps([entry_id]),),
        )

    def find_schema(self, owner_id: str, logical_key: str) -> Entry | None:
        """Look up a schema, preferring caller-owned over _system-owned.

        Single query with CASE-based ORDER BY for predictable override
        semantics: caller's own version wins, _system is fallback.
        Soft-deleted entries are excluded.
        """
        with self._pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
            self._set_rls_context(cur, owner_id)
            cur.execute(
                _load_sql("find_schema"),
                (logical_key, owner_id, owner_id),
            )
            row = cur.fetchone()
        return self._row_to_entry(row) if row else None

    def count_records_referencing(
        self, owner_id: str, schema_logical_key: str
    ) -> tuple[int, list[str]]:
        """Count and sample-id records referencing a schema version.

        Splits schema_logical_key on the last ':' to obtain schema_ref and version.
        schema_ref may itself contain ':' (e.g. "schema:edge-manifest:1.0.0").
        Matches data.schema_ref and data.schema_version in the record entries' JSONB.
        """
        ref, _, version = schema_logical_key.rpartition(":")
        with self._pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
            self._set_rls_context(cur, owner_id)
            cur.execute(_load_sql("count_records_referencing"), (owner_id, ref, version))
            count_row = cur.fetchone()
            count = int(count_row["cnt"]) if count_row else 0
            if count == 0:
                return (0, [])
            cur.execute(_load_sql("list_records_referencing_ids"), (owner_id, ref, version))
            ids = [str(r["id"]) for r in cur.fetchall()]
        return (count, ids)

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

    def update_user_profile(
        self,
        user_id: str,
        email: str | None = None,
        display_name: str | None = None,
    ) -> None:
        """Update user profile fields if currently null (enrich on login)."""
        from .helpers import canonical_email as canon_fn

        canon = canon_fn(email) if email else None
        with self._pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
            cur.execute(
                _load_sql("update_user_profile"),
                (email, canon, display_name, user_id),
            )

    def clear(self, owner_id: str) -> None:
        with self._pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
            self._set_rls_context(cur, owner_id)
            cur.execute(_load_sql("clear_reads"), (owner_id,))
            cur.execute(_load_sql("clear_actions"), (owner_id,))
            cur.execute(_load_sql("clear_embeddings"), (owner_id,))
            cur.execute(_load_sql("clear_entries"), (owner_id,))
