"""Storage protocol for the awareness store.

Defines the Store protocol (interface) that all backends must satisfy.
The PostgresStore implementation lives in postgres_store.py.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from .schema import Entry, EntryType

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
        since: datetime | None = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> list[Entry]: ...

    def get_sources(self) -> list[str]: ...

    def get_latest_status(self, source: str) -> Entry | None: ...

    def get_active_alerts(
        self,
        source: str | None = None,
        since: datetime | None = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> list[Entry]: ...

    def get_active_suppressions(self, source: str | None = None) -> list[Entry]: ...

    def get_patterns(self, source: str | None = None) -> list[Entry]: ...

    def count_active_suppressions(self) -> int: ...

    def get_knowledge(
        self,
        tags: list[str] | None = None,
        include_history: str | None = None,
        since: datetime | None = None,
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

    def get_deleted(
        self, since: datetime | None = None, limit: int | None = None, offset: int | None = None
    ) -> list[Entry]: ...

    def restore_by_id(self, entry_id: str) -> bool: ...

    def restore_by_tags(self, tags: list[str]) -> int: ...

    def clear(self) -> None: ...
