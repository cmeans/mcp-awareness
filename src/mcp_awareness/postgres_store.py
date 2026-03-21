"""PostgreSQL storage backend for the awareness store.

Implements the Store protocol using psycopg (sync driver) with JSONB
for tags and data, GIN indexes for fast tag queries, and native
concurrency (no application-level locking).

Requires: pip install psycopg[binary]
"""

from __future__ import annotations

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
    def __init__(self, dsn: str) -> None:
        self.dsn = dsn
        self._conn = psycopg.connect(dsn, row_factory=dict_row, autocommit=False)
        self._last_cleanup: float = 0.0
        self._cleanup_interval: float = 10.0
        self._create_tables()

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
            """)
        # Migration: add logical_key column if missing (existing databases)
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'entries' AND column_name = 'logical_key'"
        )
        if not cur.fetchone():
            cur.execute("ALTER TABLE entries ADD COLUMN logical_key TEXT")
        # Always ensure the index exists (covers both fresh and migrated DBs)
        cur.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_entries_source_logical_key "
            "ON entries(source, logical_key) WHERE logical_key IS NOT NULL"
        )
        self._conn.commit()

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
        """Schedule cleanup of expired entries on a background thread (debounced)."""
        now = time.monotonic()
        if now - self._last_cleanup < self._cleanup_interval:
            return
        self._last_cleanup = now
        thread = threading.Thread(target=self._do_cleanup, name="awareness-pg-cleanup", daemon=True)
        thread.start()

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
        except Exception:
            pass  # best-effort cleanup — next debounce window will retry

    _ACTIVE = "deleted IS NULL"

    def _query_entries(self, where: str = "1=1", params: tuple[Any, ...] = ()) -> list[Entry]:
        with self._conn.cursor() as cur:
            cur.execute(f"SELECT * FROM entries WHERE {self._ACTIVE} AND ({where})", params)
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
            "type = %s AND source = %s",
            (EntryType.ALERT.value, source),
        )
        for e in existing:
            if e.data.get("alert_id") == alert_id:
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
            "type = %s",
            (EntryType.PREFERENCE.value,),
        )
        for e in existing:
            if e.data.get("key") == key and e.data.get("scope") == scope:
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
        where = " AND ".join(clauses) if clauses else "1=1"
        return self._query_entries(where, tuple(params))

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

    def get_active_alerts(self, source: str | None = None) -> list[Entry]:
        clauses = ["type = %s"]
        params: list[str] = [EntryType.ALERT.value]
        if source:
            clauses.append("source = %s")
            params.append(source)
        where = " AND ".join(clauses)
        alerts = self._query_entries(where, tuple(params))
        return [a for a in alerts if not a.data.get("resolved")]

    def get_active_suppressions(self, source: str | None = None) -> list[Entry]:
        entries = self._query_entries("type = %s", (EntryType.SUPPRESSION.value,))
        if source:
            entries = [s for s in entries if s.source == source or s.source == ""]
        return entries

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
        self, tags: list[str] | None = None, include_history: str | None = None
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
        if tags:
            tag_clauses = ["tags @> %s::jsonb" for _ in tags]
            clauses.append(f"({' OR '.join(tag_clauses)})")
            params.extend(json.dumps([t]) for t in tags)
        where = " AND ".join(clauses)
        entries = self._query_entries(where, tuple(params))
        if include_history == "only":
            entries = [e for e in entries if e.data.get("changelog")]
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

    def get_deleted(self) -> list[Entry]:
        """Get all soft-deleted entries (the trash)."""
        with self._conn.cursor() as cur:
            cur.execute("SELECT * FROM entries WHERE deleted IS NOT NULL")
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

    def clear(self) -> None:
        with self._conn.cursor() as cur:
            cur.execute("DELETE FROM entries")
        self._conn.commit()
