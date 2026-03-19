"""Storage backend for the awareness store — SQLite with WAL mode."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .schema import Entry, EntryType, make_id, now_iso


class AwarenessStore:
    def __init__(self, path: str | Path = "awareness.db") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path))
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.row_factory = sqlite3.Row
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
                tags     TEXT NOT NULL DEFAULT '[]',
                data     TEXT NOT NULL DEFAULT '{}'
            );
            CREATE INDEX IF NOT EXISTS idx_entries_type ON entries(type);
            CREATE INDEX IF NOT EXISTS idx_entries_source ON entries(source);
            CREATE INDEX IF NOT EXISTS idx_entries_type_source ON entries(type, source);
        """)
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
            created=row["created"],
            updated=row["updated"],
            expires=row["expires"],
            data=json.loads(row["data"]),
        )

    def _insert_entry(self, entry: Entry) -> None:
        self._conn.execute(
            """INSERT INTO entries (id, type, source, created, updated, expires, tags, data)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                entry.id,
                entry.type.value if isinstance(entry.type, EntryType) else entry.type,
                entry.source,
                entry.created,
                entry.updated,
                entry.expires,
                json.dumps(entry.tags),
                json.dumps(entry.data),
            ),
        )

    def _cleanup_expired(self) -> None:
        """Delete entries whose expires timestamp is in the past."""
        now = datetime.now(timezone.utc).isoformat()
        self._conn.execute(
            "DELETE FROM entries WHERE expires IS NOT NULL AND expires <= ?",
            (now,),
        )
        self._conn.commit()

    def _query_entries(self, where: str = "1=1", params: tuple[Any, ...] = ()) -> list[Entry]:
        cur = self._conn.execute(f"SELECT * FROM entries WHERE {where}", params)
        return [self._row_to_entry(r) for r in cur.fetchall()]

    # ------------------------------------------------------------------
    # Public API — identical signatures to the original JSON backend
    # ------------------------------------------------------------------

    def add(self, entry: Entry) -> Entry:
        self._cleanup_expired()
        self._insert_entry(entry)
        self._conn.commit()
        return entry

    def upsert_status(self, source: str, tags: list[str], data: dict[str, Any]) -> Entry:
        """Upsert a status entry for a source (one active status per source)."""
        self._cleanup_expired()
        now = now_iso()
        self._conn.execute(
            "DELETE FROM entries WHERE type = ? AND source = ?",
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
        self._cleanup_expired()
        now = now_iso()
        existing = self._query_entries(
            "type = ? AND source = ?",
            (EntryType.ALERT.value, source),
        )
        for e in existing:
            if e.data.get("alert_id") == alert_id:
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

    def get_entries(
        self,
        entry_type: EntryType | None = None,
        source: str | None = None,
        tags: list[str] | None = None,
    ) -> list[Entry]:
        self._cleanup_expired()
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
        return results

    def get_sources(self) -> list[str]:
        """Get all unique sources that have reported status."""
        cur = self._conn.execute(
            "SELECT DISTINCT source FROM entries WHERE type = ?",
            (EntryType.STATUS.value,),
        )
        return [row["source"] for row in cur.fetchall()]

    def get_latest_status(self, source: str) -> Entry | None:
        cur = self._conn.execute(
            "SELECT * FROM entries WHERE type = ? AND source = ? ORDER BY rowid DESC LIMIT 1",
            (EntryType.STATUS.value, source),
        )
        row = cur.fetchone()
        return self._row_to_entry(row) if row else None

    def get_active_alerts(self, source: str | None = None) -> list[Entry]:
        self._cleanup_expired()
        clauses = ["type = ?"]
        params: list[str] = [EntryType.ALERT.value]
        if source:
            clauses.append("source = ?")
            params.append(source)
        where = " AND ".join(clauses)
        alerts = self._query_entries(where, tuple(params))
        return [a for a in alerts if not a.data.get("resolved")]

    def get_active_suppressions(self, source: str | None = None) -> list[Entry]:
        self._cleanup_expired()
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
        self._cleanup_expired()
        cur = self._conn.execute(
            "SELECT COUNT(*) FROM entries WHERE type = ?",
            (EntryType.SUPPRESSION.value,),
        )
        result: int = cur.fetchone()[0]
        return result

    def get_knowledge(self, tags: list[str] | None = None) -> list[Entry]:
        """Get knowledge entries (patterns, context, preferences)."""
        types = (EntryType.PATTERN.value, EntryType.CONTEXT.value, EntryType.PREFERENCE.value)
        placeholders = ",".join("?" * len(types))
        entries = self._query_entries(f"type IN ({placeholders})", types)
        if tags:
            entries = [e for e in entries if any(t in e.tags for t in tags)]
        return entries

    def clear(self) -> None:
        self._conn.execute("DELETE FROM entries")
        self._conn.commit()

    # ------------------------------------------------------------------
    # Backward-compat shims: some tests use store._entries.append() + store._save()
    # ------------------------------------------------------------------

    class _EntryList(list[Entry]):
        """A list subclass that tracks entries appended since last _save()."""

        def __init__(self, entries: list[Entry]) -> None:
            super().__init__(entries)
            self._pending: list[Entry] = []

        def append(self, entry: Entry) -> None:
            super().append(entry)
            self._pending.append(entry)

    @property
    def _entries(self) -> _EntryList:
        """Return all DB entries as a list (with append tracking for _save)."""
        if not hasattr(self, "_entry_list_cache"):
            self._entry_list_cache = self._EntryList(self._query_entries())
        return self._entry_list_cache

    def _save(self) -> None:
        """Flush any entries appended via _entries.append() to the DB."""
        if hasattr(self, "_entry_list_cache"):
            for entry in self._entry_list_cache._pending:
                self._insert_entry(entry)
            self._entry_list_cache._pending.clear()
            self._conn.commit()
            del self._entry_list_cache
