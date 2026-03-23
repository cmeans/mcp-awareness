"""Storage backend for the awareness store.

Defines the Store protocol (interface) and the default SQLiteStore implementation.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from .schema import Entry, EntryType, ensure_dt, ensure_dt_optional, make_id, now_utc, to_iso

# How long soft-deleted entries remain recoverable before auto-purge
TRASH_RETENTION_DAYS = 30


@runtime_checkable
class Store(Protocol):
    """Storage protocol — the contract that all backends must satisfy."""

    def add(self, entry: Entry) -> Entry: ...

    def upsert_status(self, source: str, tags: list[str], data: dict[str, Any]) -> Entry: ...

    def upsert_alert(
        self, source: str, tags: list[str], alert_id: str, data: dict[str, Any]
    ) -> Entry: ...

    def upsert_preference(
        self, key: str, scope: str, tags: list[str], data: dict[str, Any]
    ) -> Entry: ...

    def get_entries(
        self,
        entry_type: EntryType | None = None,
        source: str | None = None,
        tags: list[str] | None = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> list[Entry]: ...

    def get_sources(self) -> list[str]: ...

    def get_latest_status(self, source: str) -> Entry | None: ...

    def get_active_alerts(
        self, source: str | None = None, limit: int | None = None, offset: int | None = None
    ) -> list[Entry]: ...

    def get_active_suppressions(self, source: str | None = None) -> list[Entry]: ...

    def get_patterns(self, source: str | None = None) -> list[Entry]: ...

    def count_active_suppressions(self) -> int: ...

    def get_knowledge(
        self,
        tags: list[str] | None = None,
        include_history: str | None = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> list[Entry]: ...

    def get_entry_by_id(self, entry_id: str) -> Entry | None: ...

    def update_entry(self, entry_id: str, updates: dict[str, Any]) -> Entry | None: ...

    def upsert_by_logical_key(
        self, source: str, logical_key: str, entry: Entry
    ) -> tuple[Entry, bool]: ...

    def get_stats(self) -> dict[str, Any]: ...

    def get_tags(self) -> list[dict[str, Any]]: ...

    def soft_delete_by_id(self, entry_id: str) -> bool: ...

    def soft_delete_by_tags(self, tags: list[str]) -> int: ...

    def soft_delete_by_source(self, source: str, entry_type: EntryType | None = None) -> int: ...

    def get_deleted(self, limit: int | None = None, offset: int | None = None) -> list[Entry]: ...

    def restore_by_id(self, entry_id: str) -> bool: ...

    def restore_by_tags(self, tags: list[str]) -> int: ...

    def clear(self) -> None: ...


class SQLiteStore:
    def __init__(self, path: str | Path = "awareness.db") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path))
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.row_factory = sqlite3.Row
        self._write_lock = threading.Lock()
        self._last_cleanup: float = 0.0
        self._cleanup_interval: float = 10.0
        self._create_tables()

    def _create_tables(self) -> None:
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS entries (
                id       TEXT PRIMARY KEY,
                type     TEXT NOT NULL,
                source   TEXT NOT NULL,
                created  TEXT NOT NULL,
                updated  TEXT NOT NULL,
                expires  TEXT,
                deleted  TEXT,
                tags     TEXT NOT NULL DEFAULT '[]',
                data     TEXT NOT NULL DEFAULT '{}',
                logical_key TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_entries_type ON entries(type);
            CREATE INDEX IF NOT EXISTS idx_entries_source ON entries(source);
            CREATE INDEX IF NOT EXISTS idx_entries_type_source ON entries(type, source);
        """)
        # Migrations for existing databases
        cur = self._conn.execute("PRAGMA table_info(entries)")
        columns = {row["name"] for row in cur.fetchall()}
        if "deleted" not in columns:
            self._conn.execute("ALTER TABLE entries ADD COLUMN deleted TEXT")
        if "logical_key" not in columns:
            self._conn.execute("ALTER TABLE entries ADD COLUMN logical_key TEXT")
        # Always ensure the index exists (covers both fresh and migrated DBs)
        self._conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_entries_source_logical_key "
            "ON entries(source, logical_key) WHERE logical_key IS NOT NULL"
        )
        self._conn.commit()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _row_to_entry(row: sqlite3.Row) -> Entry:
        return Entry(
            id=row["id"],
            type=EntryType(row["type"]),
            source=row["source"],
            tags=json.loads(row["tags"]),
            created=ensure_dt(row["created"]),
            updated=ensure_dt(row["updated"]),
            expires=ensure_dt_optional(row["expires"]),
            data=json.loads(row["data"]),
            logical_key=row["logical_key"] if "logical_key" in dict(row) else None,
        )

    def _insert_entry(self, entry: Entry) -> None:
        self._conn.execute(
            """INSERT INTO entries
               (id, type, source, created, updated, expires, tags, data, logical_key)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                entry.id,
                entry.type.value if isinstance(entry.type, EntryType) else entry.type,
                entry.source,
                to_iso(entry.created),
                to_iso(entry.updated),
                to_iso(entry.expires) if entry.expires else None,
                json.dumps(entry.tags),
                json.dumps(entry.data),
                entry.logical_key,
            ),
        )

    def _cleanup_expired(self) -> None:
        """Schedule cleanup of expired entries on a background thread (debounced).

        Never blocks the calling request. The actual DELETE runs in a
        separate thread with its own SQLite connection.
        """
        now = time.monotonic()
        if now - self._last_cleanup < self._cleanup_interval:
            return
        self._last_cleanup = now  # claim the slot immediately to prevent races
        thread = threading.Thread(target=self._do_cleanup, name="awareness-cleanup", daemon=True)
        thread.start()

    def _do_cleanup(self) -> None:
        """Run the actual DELETE on a dedicated connection (background thread)."""
        try:
            conn = sqlite3.connect(str(self.path))
            now = datetime.now(timezone.utc).isoformat()
            conn.execute(
                "DELETE FROM entries WHERE expires IS NOT NULL AND expires <= ?",
                (now,),
            )
            conn.commit()
            conn.close()
        except Exception as exc:
            print(f"[awareness] cleanup failed: {type(exc).__name__}: {exc}")

    # Base filter for all normal reads — excludes soft-deleted entries
    _ACTIVE = "deleted IS NULL"

    def _query_entries(self, where: str = "1=1", params: tuple[Any, ...] = ()) -> list[Entry]:
        cur = self._conn.execute(
            f"SELECT * FROM entries WHERE {self._ACTIVE} AND ({where})", params
        )
        return [self._row_to_entry(r) for r in cur.fetchall()]

    @staticmethod
    def _paginate(entries: list[Entry], limit: int | None, offset: int | None) -> list[Entry]:
        """Apply offset and limit to a list of entries."""
        if offset:
            entries = entries[offset:]
        if limit:
            entries = entries[:limit]
        return entries

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def add(self, entry: Entry) -> Entry:
        with self._write_lock:
            self._cleanup_expired()
            self._insert_entry(entry)
            self._conn.commit()
            return entry

    def upsert_status(self, source: str, tags: list[str], data: dict[str, Any]) -> Entry:
        """Upsert a status entry for a source (one active status per source)."""
        with self._write_lock:
            self._cleanup_expired()
            now = now_utc()
            self._conn.execute(
                f"DELETE FROM entries WHERE type = ? AND source = ? AND {self._ACTIVE}",
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
            self._insert_entry(entry)
            self._conn.commit()
            return entry

    def upsert_alert(
        self, source: str, tags: list[str], alert_id: str, data: dict[str, Any]
    ) -> Entry:
        """Upsert an alert by source + alert_id."""
        with self._write_lock:
            self._cleanup_expired()
            now = now_utc()
            existing = self._query_entries(
                "type = ? AND source = ? AND json_extract(data, '$.alert_id') = ?",
                (EntryType.ALERT.value, source, alert_id),
            )
            if existing:
                e = existing[0]
                e.updated = now
                e.tags = tags
                e.data.update(data)
                self._conn.execute(
                    """UPDATE entries SET updated = ?, tags = ?, data = ?
                       WHERE id = ?""",
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
            self._insert_entry(entry)
            self._conn.commit()
            return entry

    def upsert_preference(
        self, key: str, scope: str, tags: list[str], data: dict[str, Any]
    ) -> Entry:
        """Upsert a preference by key + scope."""
        with self._write_lock:
            self._cleanup_expired()
            now = now_utc()
            existing = self._query_entries(
                "type = ? AND json_extract(data, '$.key') = ?"
                " AND json_extract(data, '$.scope') = ?",
                (EntryType.PREFERENCE.value, key, scope),
            )
            if existing:
                e = existing[0]
                e.updated = now
                e.tags = tags
                e.data.update(data)
                self._conn.execute(
                    """UPDATE entries SET updated = ?, tags = ?, data = ?
                       WHERE id = ?""",
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
            self._insert_entry(entry)
            self._conn.commit()
            return entry

    def get_entries(
        self,
        entry_type: EntryType | None = None,
        source: str | None = None,
        tags: list[str] | None = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> list[Entry]:
        clauses: list[str] = []
        params: list[str] = []
        if entry_type is not None:
            clauses.append("type = ?")
            params.append(entry_type.value)
        if source is not None:
            clauses.append("source = ?")
            params.append(source)
        where = " AND ".join(clauses) if clauses else "1=1"
        results = self._query_entries(where, tuple(params))
        if tags:
            results = [e for e in results if any(t in e.tags for t in tags)]
        return self._paginate(results, limit, offset)

    def get_sources(self) -> list[str]:
        """Get all unique sources that have reported status."""
        cur = self._conn.execute(
            f"SELECT DISTINCT source FROM entries WHERE type = ? AND {self._ACTIVE}",
            (EntryType.STATUS.value,),
        )
        return [row["source"] for row in cur.fetchall()]

    def get_latest_status(self, source: str) -> Entry | None:
        cur = self._conn.execute(
            f"SELECT * FROM entries WHERE type = ? AND source = ? AND {self._ACTIVE}"
            " ORDER BY rowid DESC LIMIT 1",
            (EntryType.STATUS.value, source),
        )
        row = cur.fetchone()
        return self._row_to_entry(row) if row else None

    def get_active_alerts(
        self, source: str | None = None, limit: int | None = None, offset: int | None = None
    ) -> list[Entry]:
        clauses = ["type = ?"]
        params: list[str] = [EntryType.ALERT.value]
        if source:
            clauses.append("source = ?")
            params.append(source)
        where = " AND ".join(clauses)
        alerts = self._query_entries(where, tuple(params))
        results = [a for a in alerts if not a.data.get("resolved")]
        return self._paginate(results, limit, offset)

    def get_active_suppressions(self, source: str | None = None) -> list[Entry]:
        entries = self._query_entries("type = ?", (EntryType.SUPPRESSION.value,))
        if source:
            entries = [s for s in entries if s.source == source or s.source == ""]
        return entries

    def get_patterns(self, source: str | None = None) -> list[Entry]:
        if source:
            return self._query_entries(
                "type = ? AND source = ?",
                (EntryType.PATTERN.value, source),
            )
        return self._query_entries("type = ?", (EntryType.PATTERN.value,))

    def count_active_suppressions(self) -> int:
        cur = self._conn.execute(
            f"SELECT COUNT(*) FROM entries WHERE type = ? AND {self._ACTIVE}",
            (EntryType.SUPPRESSION.value,),
        )
        result: int = cur.fetchone()[0]
        return result

    def get_knowledge(
        self,
        tags: list[str] | None = None,
        include_history: str | None = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> list[Entry]:
        """Get knowledge entries (patterns, context, preferences, notes).

        include_history: None/false = strip changelog from results,
                         "true" = include changelog, "only" = only entries with changelog.
        """
        types = (
            EntryType.PATTERN.value,
            EntryType.CONTEXT.value,
            EntryType.PREFERENCE.value,
            EntryType.NOTE.value,
        )
        placeholders = ",".join("?" * len(types))
        entries = self._query_entries(f"type IN ({placeholders})", types)
        if tags:
            entries = [e for e in entries if any(t in e.tags for t in tags)]
        if include_history == "only":
            entries = [e for e in entries if e.data.get("changelog")]
        elif include_history != "true":
            # Strip changelog from results by default
            for e in entries:
                e.data.pop("changelog", None)
        return self._paginate(entries, limit, offset)

    def get_entry_by_id(self, entry_id: str) -> Entry | None:
        """Get a single entry by ID (active only)."""
        results = self._query_entries("id = ?", (entry_id,))
        return results[0] if results else None

    def update_entry(self, entry_id: str, updates: dict[str, Any]) -> Entry | None:
        """Update an entry in place, appending previous values to changelog.

        Only works on knowledge types (note, pattern, context, preference).
        Returns the updated entry, or None if not found or type is immutable.
        """
        entry = self.get_entry_by_id(entry_id)
        if entry is None:
            return None
        immutable = {EntryType.STATUS, EntryType.ALERT, EntryType.SUPPRESSION}
        if entry.type in immutable:
            return None

        with self._write_lock:
            self._cleanup_expired()
            now = now_utc()
            # Build changelog record of changed fields
            changed: dict[str, Any] = {}
            # Envelope fields
            for field in ("source", "tags"):
                if field in updates and updates[field] != getattr(entry, field):
                    changed[field] = getattr(entry, field)
                    setattr(entry, field, updates[field])
            # Data fields
            for field in ("description", "content", "content_type"):
                if field in updates and updates[field] != entry.data.get(field):
                    old_val = entry.data.get(field)
                    if old_val is not None:
                        changed[field] = old_val
                    entry.data[field] = updates[field]

            if not changed:
                return entry  # nothing actually changed

            # Append to changelog (ISO string in JSON data)
            changelog = entry.data.setdefault("changelog", [])
            changelog.append({"updated": to_iso(now), "changed": changed})
            entry.updated = now

            self._conn.execute(
                "UPDATE entries SET updated = ?, source = ?, tags = ?, data = ? WHERE id = ?",
                (
                    to_iso(now),
                    entry.source,
                    json.dumps(entry.tags),
                    json.dumps(entry.data),
                    entry.id,
                ),
            )
            self._conn.commit()
            return entry

    def upsert_by_logical_key(
        self, source: str, logical_key: str, entry: Entry
    ) -> tuple[Entry, bool]:
        """Upsert by source + logical_key. Returns (entry, created).

        If an entry with the same source + logical_key exists, updates it
        (with changelog tracking). Otherwise inserts the new entry.
        """
        existing = self._query_entries("source = ? AND logical_key = ?", (source, logical_key))
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
        with self._write_lock:
            self._cleanup_expired()
            self._insert_entry(entry)
            self._conn.commit()
        return (entry, True)

    def get_stats(self) -> dict[str, Any]:
        """Get entry counts by type, list of sources, and total count."""
        cur = self._conn.execute(
            f"SELECT type, COUNT(*) FROM entries WHERE {self._ACTIVE} GROUP BY type"
        )
        counts = {row[0]: row[1] for row in cur.fetchall()}
        cur2 = self._conn.execute(
            f"SELECT DISTINCT source FROM entries WHERE {self._ACTIVE} ORDER BY source"
        )
        sources = [row[0] for row in cur2.fetchall()]
        return {
            "entries": {t.value: counts.get(t.value, 0) for t in EntryType},
            "sources": sources,
            "total": sum(counts.values()),
        }

    def get_tags(self) -> list[dict[str, Any]]:
        """Get all tags in use with usage counts."""
        cur = self._conn.execute(
            f"SELECT value, COUNT(*) as cnt FROM entries, json_each(entries.tags) "
            f"WHERE {self._ACTIVE} GROUP BY value ORDER BY cnt DESC"
        )
        return [{"tag": row[0], "count": row[1]} for row in cur.fetchall()]

    # ------------------------------------------------------------------
    # Soft delete / trash
    # ------------------------------------------------------------------

    def soft_delete_by_id(self, entry_id: str) -> bool:
        """Soft-delete a single entry. Returns True if an entry was trashed."""
        with self._write_lock:
            now = datetime.now(timezone.utc)
            expires = (now + timedelta(days=TRASH_RETENTION_DAYS)).isoformat()
            cur = self._conn.execute(
                f"UPDATE entries SET deleted = ?, expires = ? WHERE id = ? AND {self._ACTIVE}",
                (now.isoformat(), expires, entry_id),
            )
            self._conn.commit()
            return cur.rowcount > 0

    def soft_delete_by_tags(self, tags: list[str]) -> int:
        """Soft-delete all entries matching ALL given tags (AND logic).

        Returns the number of trashed entries.
        """
        if not tags:
            return 0
        with self._write_lock:
            now = datetime.now(timezone.utc)
            expires = (now + timedelta(days=TRASH_RETENTION_DAYS)).isoformat()
            # AND: entry must contain every tag — count matching tags per entry
            placeholders = ",".join("?" * len(tags))
            cur = self._conn.execute(
                f"UPDATE entries SET deleted = ?, expires = ? WHERE {self._ACTIVE} "
                f"AND id IN ("
                f"  SELECT e.id FROM entries e, json_each(e.tags) t"
                f"  WHERE t.value IN ({placeholders}) AND e.{self._ACTIVE}"
                f"  GROUP BY e.id HAVING COUNT(DISTINCT t.value) = ?"
                f")",
                (now.isoformat(), expires, *tags, len(tags)),
            )
            self._conn.commit()
            return cur.rowcount

    def soft_delete_by_source(
        self,
        source: str,
        entry_type: EntryType | None = None,
    ) -> int:
        """Soft-delete all entries for a source, optionally filtered by type.

        Returns the number of trashed entries.
        """
        with self._write_lock:
            now = datetime.now(timezone.utc)
            expires = (now + timedelta(days=TRASH_RETENTION_DAYS)).isoformat()
            clauses = ["source = ?", self._ACTIVE]
            params: list[str] = [source]
            if entry_type is not None:
                clauses.append("type = ?")
                params.append(entry_type.value)
            where = " AND ".join(clauses)
            cur = self._conn.execute(
                f"UPDATE entries SET deleted = ?, expires = ? WHERE {where}",
                (now.isoformat(), expires, *params),
            )
            self._conn.commit()
            return cur.rowcount

    def get_deleted(self, limit: int | None = None, offset: int | None = None) -> list[Entry]:
        """Get all soft-deleted entries (the trash)."""
        cur = self._conn.execute("SELECT * FROM entries WHERE deleted IS NOT NULL")
        results = [self._row_to_entry(r) for r in cur.fetchall()]
        return self._paginate(results, limit, offset)

    def restore_by_id(self, entry_id: str) -> bool:
        """Restore a soft-deleted entry. Returns True if restored."""
        with self._write_lock:
            cur = self._conn.execute(
                "UPDATE entries SET deleted = NULL, expires = NULL"
                " WHERE id = ? AND deleted IS NOT NULL",
                (entry_id,),
            )
            self._conn.commit()
            return cur.rowcount > 0

    def restore_by_tags(self, tags: list[str]) -> int:
        """Restore all soft-deleted entries matching ALL given tags (AND logic).

        Returns the number of restored entries.
        """
        if not tags:
            return 0
        with self._write_lock:
            placeholders = ",".join("?" * len(tags))
            cur = self._conn.execute(
                "UPDATE entries SET deleted = NULL, expires = NULL "
                "WHERE deleted IS NOT NULL "
                "AND id IN ("
                "  SELECT e.id FROM entries e, json_each(e.tags) t"
                "  WHERE t.value IN (" + placeholders + ") AND e.deleted IS NOT NULL"
                "  GROUP BY e.id HAVING COUNT(DISTINCT t.value) = ?"
                ")",
                (*tags, len(tags)),
            )
            self._conn.commit()
            return cur.rowcount

    def clear(self) -> None:
        with self._write_lock:
            self._conn.execute("DELETE FROM entries")
            self._conn.commit()


# Backward-compatibility alias
AwarenessStore = SQLiteStore
