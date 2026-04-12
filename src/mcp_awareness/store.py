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

    def add(self, owner_id: str, entry: Entry) -> Entry:
        """Store a new entry and return it."""
        ...

    def upsert_status(
        self, owner_id: str, source: str, tags: list[str], data: dict[str, Any]
    ) -> Entry:
        """Upsert a status entry for a source (one active status per source)."""
        ...

    def upsert_alert(
        self, owner_id: str, source: str, tags: list[str], alert_id: str, data: dict[str, Any]
    ) -> Entry:
        """Upsert an alert keyed by source + alert_id."""
        ...

    def upsert_preference(
        self, owner_id: str, key: str, scope: str, tags: list[str], data: dict[str, Any]
    ) -> Entry:
        """Upsert a preference keyed by key + scope."""
        ...

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
        """Query active entries with optional filters for type, source, tags, and time."""
        ...

    def get_sources(self, owner_id: str) -> list[str]:
        """Return all unique sources that have reported status."""
        ...

    def get_latest_status(self, owner_id: str, source: str) -> Entry | None:
        """Get the most recent active status entry for a source, or None."""
        ...

    def get_active_alerts(
        self,
        owner_id: str,
        source: str | None = None,
        since: datetime | None = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> list[Entry]:
        """Get non-expired alert entries, optionally filtered by source and time."""
        ...

    def get_active_suppressions(self, owner_id: str, source: str | None = None) -> list[Entry]:
        """Get non-expired suppression entries, optionally filtered by source."""
        ...

    def get_patterns(self, owner_id: str, source: str | None = None) -> list[Entry]:
        """Get all pattern entries, optionally filtered by source."""
        ...

    def get_all_statuses(self, owner_id: str) -> dict[str, Entry]:
        """Get latest status for every source. Returns {source: Entry}."""
        ...

    def get_all_active_alerts(self, owner_id: str) -> dict[str, list[Entry]]:
        """Get all non-resolved alerts grouped by source. Returns {source: [Entry]}."""
        ...

    def get_all_active_suppressions(self, owner_id: str) -> dict[str, list[Entry]]:
        """Get all active suppressions grouped by source.

        Includes global suppressions (empty source) under key ''.
        """
        ...

    def get_all_patterns(self, owner_id: str) -> dict[str, list[Entry]]:
        """Get all patterns grouped by source. Includes global (empty source) under key ''."""
        ...

    def count_active_suppressions(self, owner_id: str) -> int:
        """Return the count of non-expired suppression entries."""
        ...

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
        """Query knowledge entries (note, pattern, context, preference) with rich filtering."""
        ...

    def get_entry_by_id(self, owner_id: str, entry_id: str) -> Entry | None:
        """Get a single active entry by ID, or None if not found or deleted."""
        ...

    def get_entries_by_ids(self, owner_id: str, entry_ids: list[str]) -> list[Entry]:
        """Get multiple active entries by ID in a single query."""
        ...

    def update_entry(self, owner_id: str, entry_id: str, updates: dict[str, Any]) -> Entry | None:
        """Update a knowledge entry in place, appending previous values to changelog."""
        ...

    def upsert_by_logical_key(
        self, owner_id: str, source: str, logical_key: str, entry: Entry
    ) -> tuple[Entry, bool]:
        """Upsert by source + logical_key. Returns (entry, created)."""
        ...

    def get_stats(self, owner_id: str) -> dict[str, Any]:
        """Get entry counts by type, list of sources, and total count."""
        ...

    def get_tags(self, owner_id: str) -> list[dict[str, Any]]:
        """Get all tags in use with usage counts."""
        ...

    def soft_delete_by_id(self, owner_id: str, entry_id: str) -> bool:
        """Soft-delete a single entry. Returns True if an entry was trashed."""
        ...

    def soft_delete_by_tags(self, owner_id: str, tags: list[str]) -> int:
        """Soft-delete all entries matching ALL given tags. Returns count trashed."""
        ...

    def soft_delete_by_source(
        self, owner_id: str, source: str, entry_type: EntryType | None = None
    ) -> int:
        """Soft-delete all entries for a source, optionally by type. Returns count trashed."""
        ...

    def get_deleted(
        self,
        owner_id: str,
        since: datetime | None = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> list[Entry]:
        """Get all soft-deleted entries (the trash), ordered by deletion time."""
        ...

    def restore_by_id(self, owner_id: str, entry_id: str) -> bool:
        """Restore a soft-deleted entry. Returns True if restored."""
        ...

    def restore_by_tags(self, owner_id: str, tags: list[str]) -> int:
        """Restore all soft-deleted entries matching ALL given tags. Returns count restored."""
        ...

    # Read / action tracking

    def log_read(
        self,
        owner_id: str,
        entry_ids: list[str],
        tool_used: str,
        platform: str | None = None,
    ) -> None:
        """Record that entries were read. Fire-and-forget — failures are silent."""
        ...

    def log_action(
        self,
        owner_id: str,
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
        owner_id: str,
        entry_id: str | None = None,
        since: datetime | None = None,
        platform: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Query read-tracking records with optional filters."""
        ...

    def get_actions(
        self,
        owner_id: str,
        entry_id: str | None = None,
        since: datetime | None = None,
        platform: str | None = None,
        tags: list[str] | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Query action records with optional filters."""
        ...

    def get_unread(
        self, owner_id: str, since: datetime | None = None, limit: int | None = None
    ) -> list[Entry]:
        """Get entries with zero reads, optionally created since a timestamp."""
        ...

    def get_activity(
        self,
        owner_id: str,
        since: datetime | None = None,
        platform: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Get a unified timeline of reads and actions, sorted by timestamp."""
        ...

    def get_read_counts(self, owner_id: str, entry_ids: list[str]) -> dict[str, dict[str, Any]]:
        """Get read_count and last_read for a list of entry IDs."""
        ...

    # Intentions

    def get_intentions(
        self,
        owner_id: str,
        state: str | None = None,
        source: str | None = None,
        tags: list[str] | None = None,
        limit: int | None = None,
    ) -> list[Entry]:
        """Get intention entries, optionally filtered by state, source, or tags."""
        ...

    def update_intention_state(
        self, owner_id: str, entry_id: str, new_state: str, reason: str | None = None
    ) -> Entry | None:
        """Transition an intention to a new state. Appends to changelog."""
        ...

    def get_fired_intentions(self, owner_id: str) -> list[Entry]:
        """Get intentions whose deliver_at has passed and state is still pending."""
        ...

    # Embeddings / semantic search

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
        ...

    def get_entries_without_embeddings(
        self,
        owner_id: str,
        model: str,
        limit: int = 100,
    ) -> list[Entry]:
        """Find active entries that have no embedding for the given model."""
        ...

    def get_stale_embeddings(
        self,
        owner_id: str,
        model: str,
        limit: int = 100,
    ) -> list[Entry]:
        """Find entries whose text has changed since their embedding was generated."""
        ...

    def semantic_search(
        self,
        owner_id: str,
        embedding: list[float],
        model: str,
        query_text: str = "",
        query_language: str = "simple",
        entry_type: EntryType | None = None,
        source: str | None = None,
        tags: list[str] | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 10,
    ) -> list[tuple[Entry, float]]:
        """Hybrid search: vector + FTS fused via RRF. Returns (entry, score) pairs."""
        ...

    def get_referencing_entries(self, owner_id: str, entry_id: str) -> list[Entry]:
        """Find entries whose data.related_ids contains the given entry_id."""
        ...

    def clear(self, owner_id: str) -> None:
        """Delete all entries, reads, actions, and embeddings for an owner."""
        ...
