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

    def add(self, entry: Entry) -> Entry:
        """Store a new entry and return it."""
        ...

    def upsert_status(self, source: str, tags: list[str], data: dict[str, Any]) -> Entry:
        """Upsert a status entry for a source (one active status per source)."""
        ...

    def upsert_alert(
        self, source: str, tags: list[str], alert_id: str, data: dict[str, Any]
    ) -> Entry:
        """Upsert an alert keyed by source + alert_id."""
        ...

    def upsert_preference(
        self, key: str, scope: str, tags: list[str], data: dict[str, Any]
    ) -> Entry:
        """Upsert a preference keyed by key + scope."""
        ...

    def get_entries(
        self,
        entry_type: EntryType | None = None,
        source: str | None = None,
        tags: list[str] | None = None,
        since: datetime | None = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> list[Entry]:
        """Query active entries with optional filters for type, source, tags, and time."""
        ...

    def get_sources(self) -> list[str]:
        """Return all unique sources that have reported status."""
        ...

    def get_latest_status(self, source: str) -> Entry | None:
        """Get the most recent active status entry for a source, or None."""
        ...

    def get_active_alerts(
        self,
        source: str | None = None,
        since: datetime | None = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> list[Entry]:
        """Get non-expired alert entries, optionally filtered by source and time."""
        ...

    def get_active_suppressions(self, source: str | None = None) -> list[Entry]:
        """Get non-expired suppression entries, optionally filtered by source."""
        ...

    def get_patterns(self, source: str | None = None) -> list[Entry]:
        """Get all pattern entries, optionally filtered by source."""
        ...

    def count_active_suppressions(self) -> int:
        """Return the count of non-expired suppression entries."""
        ...

    def get_knowledge(
        self,
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
        """Query knowledge entries (note, pattern, context, preference) with rich filtering."""
        ...

    def get_entry_by_id(self, entry_id: str) -> Entry | None:
        """Get a single active entry by ID, or None if not found or deleted."""
        ...

    def update_entry(self, entry_id: str, updates: dict[str, Any]) -> Entry | None:
        """Update a knowledge entry in place, appending previous values to changelog."""
        ...

    def upsert_by_logical_key(
        self, source: str, logical_key: str, entry: Entry
    ) -> tuple[Entry, bool]:
        """Upsert by source + logical_key. Returns (entry, created)."""
        ...

    def get_stats(self) -> dict[str, Any]:
        """Get entry counts by type, list of sources, and total count."""
        ...

    def get_tags(self) -> list[dict[str, Any]]:
        """Get all tags in use with usage counts."""
        ...

    def soft_delete_by_id(self, entry_id: str) -> bool:
        """Soft-delete a single entry. Returns True if an entry was trashed."""
        ...

    def soft_delete_by_tags(self, tags: list[str]) -> int:
        """Soft-delete all entries matching ALL given tags. Returns count trashed."""
        ...

    def soft_delete_by_source(self, source: str, entry_type: EntryType | None = None) -> int:
        """Soft-delete all entries for a source, optionally by type. Returns count trashed."""
        ...

    def get_deleted(
        self, since: datetime | None = None, limit: int | None = None, offset: int | None = None
    ) -> list[Entry]:
        """Get all soft-deleted entries (the trash), ordered by deletion time."""
        ...

    def restore_by_id(self, entry_id: str) -> bool:
        """Restore a soft-deleted entry. Returns True if restored."""
        ...

    def restore_by_tags(self, tags: list[str]) -> int:
        """Restore all soft-deleted entries matching ALL given tags. Returns count restored."""
        ...

    # Read / action tracking

    def log_read(
        self, entry_ids: list[str], tool_used: str, platform: str | None = None
    ) -> None:
        """Record that entries were read. Fire-and-forget — failures are silent."""
        ...

    def log_action(
        self,
        entry_id: str,
        action: str,
        platform: str | None = None,
        detail: str | None = None,
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        """Record an action taken on an entry. Returns the action record."""
        ...

    def get_reads(
        self,
        entry_id: str | None = None,
        since: datetime | None = None,
        platform: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Query read-tracking records with optional filters."""
        ...

    def get_actions(
        self,
        entry_id: str | None = None,
        since: datetime | None = None,
        platform: str | None = None,
        tags: list[str] | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Query action records with optional filters."""
        ...

    def get_unread(self, since: datetime | None = None) -> list[Entry]:
        """Get entries with zero reads, optionally created since a timestamp."""
        ...

    def get_activity(
        self,
        since: datetime | None = None,
        platform: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Get a unified timeline of reads and actions, sorted by timestamp."""
        ...

    def get_read_counts(self, entry_ids: list[str]) -> dict[str, dict[str, Any]]:
        """Get read_count and last_read for a list of entry IDs."""
        ...

    # Intentions

    def get_intentions(
        self,
        state: str | None = None,
        source: str | None = None,
        tags: list[str] | None = None,
        limit: int | None = None,
    ) -> list[Entry]:
        """Get intention entries, optionally filtered by state, source, or tags."""
        ...

    def update_intention_state(
        self, entry_id: str, new_state: str, reason: str | None = None
    ) -> Entry | None:
        """Transition an intention to a new state. Appends to changelog."""
        ...

    def get_fired_intentions(self) -> list[Entry]:
        """Get intentions whose deliver_at has passed and state is still pending."""
        ...

    # Embeddings / semantic search

    def upsert_embedding(
        self,
        entry_id: str,
        model: str,
        dimensions: int,
        text_hash: str,
        embedding: list[float],
    ) -> None:
        """Store or update an embedding for an entry + model pair."""
        ...

    def get_entries_without_embeddings(
        self,
        model: str,
        limit: int = 100,
    ) -> list[Entry]:
        """Find active entries that have no embedding for the given model."""
        ...

    def get_stale_embeddings(
        self,
        model: str,
        limit: int = 100,
    ) -> list[Entry]:
        """Find entries whose text has changed since their embedding was generated."""
        ...

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
        """Search entries by vector similarity. Returns (entry, score) pairs sorted by relevance."""
        ...

    def get_referencing_entries(self, entry_id: str) -> list[Entry]:
        """Find entries whose data.related_ids contains the given entry_id."""
        ...

    def clear(self) -> None:
        """Delete all entries, reads, actions, and embeddings."""
        ...
