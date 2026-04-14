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

"""Tests for the PostgreSQL storage backend."""

import concurrent.futures
import threading
import time

import pytest

from mcp_awareness.language import SIMPLE
from mcp_awareness.schema import Entry, EntryType, make_id, now_utc

TEST_OWNER = "test-owner"

# store fixture comes from conftest.py (testcontainers Postgres)


def test_empty_store(store):
    assert store.get_sources(TEST_OWNER) == []
    assert store.get_active_alerts(TEST_OWNER) == []
    assert store.get_knowledge(TEST_OWNER) == []


def test_upsert_status(store):
    entry = store.upsert_status(
        TEST_OWNER, "nas", ["infra"], {"metrics": {"cpu": 50}, "ttl_sec": 120}
    )
    assert entry.type == EntryType.STATUS
    assert entry.source == "nas"
    assert store.get_sources(TEST_OWNER) == ["nas"]


def test_upsert_status_replaces(store):
    store.upsert_status(TEST_OWNER, "nas", ["infra"], {"metrics": {"cpu": 50}, "ttl_sec": 120})
    store.upsert_status(TEST_OWNER, "nas", ["infra"], {"metrics": {"cpu": 70}, "ttl_sec": 120})
    statuses = store.get_entries(TEST_OWNER, entry_type=EntryType.STATUS, source="nas")
    assert len(statuses) == 1
    assert statuses[0].data["metrics"]["cpu"] == 70


def test_upsert_alert_new(store):
    entry = store.upsert_alert(
        TEST_OWNER,
        "nas",
        ["infra"],
        "cpu-warn-1",
        {"alert_id": "cpu-warn-1", "level": "warning", "message": "CPU high", "resolved": False},
    )
    assert entry.type == EntryType.ALERT
    alerts = store.get_active_alerts(TEST_OWNER)
    assert len(alerts) == 1


def test_upsert_alert_updates(store):
    store.upsert_alert(
        TEST_OWNER,
        "nas",
        ["infra"],
        "cpu-warn-1",
        {"alert_id": "cpu-warn-1", "level": "warning", "message": "CPU high", "resolved": False},
    )
    store.upsert_alert(
        TEST_OWNER,
        "nas",
        ["infra"],
        "cpu-warn-1",
        {
            "alert_id": "cpu-warn-1",
            "level": "critical",
            "message": "CPU very high",
            "resolved": False,
        },
    )
    alerts = store.get_active_alerts(TEST_OWNER)
    assert len(alerts) == 1
    assert alerts[0].data["level"] == "critical"
    assert alerts[0].data["message"] == "CPU very high"


def test_resolved_alert_not_active(store):
    store.upsert_alert(
        TEST_OWNER,
        "nas",
        ["infra"],
        "cpu-warn-1",
        {"alert_id": "cpu-warn-1", "level": "warning", "message": "CPU high", "resolved": True},
    )
    assert store.get_active_alerts(TEST_OWNER) == []


def test_expired_alert_not_active(store):
    """Expired alerts are excluded from get_active_alerts."""
    from datetime import datetime, timedelta, timezone

    past = datetime.now(timezone.utc) - timedelta(hours=1)
    future = datetime.now(timezone.utc) + timedelta(hours=1)

    # Expired alert — should not appear
    expired_alert = Entry(
        id=make_id(),
        type=EntryType.ALERT,
        source="nas",
        tags=["infra"],
        created=past,
        expires=past,
        data={"alert_id": "exp-1", "level": "warning", "message": "old", "resolved": False},
    )
    store.add(TEST_OWNER, expired_alert)

    # Non-expired alert — should appear
    active_alert = Entry(
        id=make_id(),
        type=EntryType.ALERT,
        source="nas",
        tags=["infra"],
        created=past,
        expires=future,
        data={"alert_id": "act-1", "level": "warning", "message": "current", "resolved": False},
    )
    store.add(TEST_OWNER, active_alert)

    # No-expiry alert — should appear
    no_expiry = Entry(
        id=make_id(),
        type=EntryType.ALERT,
        source="nas",
        tags=["infra"],
        created=past,
        expires=None,
        data={"alert_id": "nox-1", "level": "warning", "message": "forever", "resolved": False},
    )
    store.add(TEST_OWNER, no_expiry)

    alerts = store.get_active_alerts(TEST_OWNER)
    alert_ids = [a.data["alert_id"] for a in alerts]
    assert "exp-1" not in alert_ids, "Expired alert should be filtered out"
    assert "act-1" in alert_ids, "Non-expired alert should be included"
    assert "nox-1" in alert_ids, "Alert without expiry should be included"
    assert len(alerts) == 2


def test_get_active_alerts_by_source(store):
    store.upsert_alert(
        TEST_OWNER,
        "nas",
        ["infra"],
        "a1",
        {"alert_id": "a1", "level": "warning", "message": "NAS issue", "resolved": False},
    )
    store.upsert_alert(
        TEST_OWNER,
        "ci",
        ["cicd"],
        "a2",
        {"alert_id": "a2", "level": "warning", "message": "CI issue", "resolved": False},
    )
    assert len(store.get_active_alerts(TEST_OWNER, "nas")) == 1
    assert len(store.get_active_alerts(TEST_OWNER, "ci")) == 1
    assert len(store.get_active_alerts(TEST_OWNER, "other")) == 0


def test_add_and_get_patterns(store):
    now = now_utc()
    entry = Entry(
        id=make_id(),
        type=EntryType.PATTERN,
        source="nas",
        tags=["infra"],
        created=now,
        expires=None,
        data={
            "description": "test",
            "conditions": {},
            "effect": "suppress test",
            "learned_from": "test",
        },
    )
    store.add(TEST_OWNER, entry)
    patterns = store.get_patterns(TEST_OWNER, "nas")
    assert len(patterns) == 1
    assert patterns[0].data["description"] == "test"


def test_get_patterns_filtered_by_source(store):
    now = now_utc()
    for src in ("nas", "ci"):
        store.add(
            TEST_OWNER,
            Entry(
                id=make_id(),
                type=EntryType.PATTERN,
                source=src,
                tags=[],
                created=now,
                expires=None,
                data={
                    "description": f"{src} pattern",
                    "conditions": {},
                    "effect": "",
                    "learned_from": "test",
                },
            ),
        )
    assert len(store.get_patterns(TEST_OWNER, "nas")) == 1
    assert len(store.get_patterns(TEST_OWNER, "ci")) == 1
    assert len(store.get_patterns(TEST_OWNER)) == 2


def test_suppressions(store):
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    expires = now + timedelta(hours=1)
    entry = Entry(
        id=make_id(),
        type=EntryType.SUPPRESSION,
        source="nas",
        tags=["infra"],
        created=now,
        expires=expires,
        data={
            "metric": "cpu_pct",
            "suppress_level": "warning",
            "escalation_override": True,
            "reason": "test",
        },
    )
    store.add(TEST_OWNER, entry)
    assert store.count_active_suppressions(TEST_OWNER) == 1
    assert len(store.get_active_suppressions(TEST_OWNER, "nas")) == 1


def test_global_suppression_matches_any_source(store):
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    expires = now + timedelta(hours=1)
    entry = Entry(
        id=make_id(),
        type=EntryType.SUPPRESSION,
        source="",
        tags=[],
        created=now,
        expires=expires,
        data={
            "metric": None,
            "suppress_level": "warning",
            "escalation_override": True,
            "reason": "global",
        },
    )
    store.add(TEST_OWNER, entry)
    assert len(store.get_active_suppressions(TEST_OWNER, "nas")) == 1
    assert len(store.get_active_suppressions(TEST_OWNER, "ci")) == 1


def _opt_in_cleanup(store, owner_id: str) -> None:
    """Helper: opt an owner in to auto-cleanup via preference."""
    store.upsert_preference(
        owner_id,
        key="auto_cleanup",
        scope="global",
        tags=[],
        data={"key": "auto_cleanup", "value": "true", "scope": "global"},
    )


def test_expired_entries_cleaned_when_opted_in(store):
    from datetime import datetime, timedelta, timezone

    _opt_in_cleanup(store, TEST_OWNER)
    past = datetime.now(timezone.utc) - timedelta(hours=1)
    entry = Entry(
        id=make_id(),
        type=EntryType.SUPPRESSION,
        source="nas",
        tags=[],
        created=past,
        expires=past,
        data={"metric": "cpu_pct", "suppress_level": "warning"},
    )
    store.add(TEST_OWNER, entry)
    store._do_cleanup()
    # Entry purged — owner opted in
    assert store.get_entry_by_id(TEST_OWNER, entry.id) is None


def test_expired_entries_kept_when_not_opted_in(store):
    """Without auto_cleanup preference, expired entries are retained."""
    from datetime import datetime, timedelta, timezone

    past = datetime.now(timezone.utc) - timedelta(hours=1)
    entry = Entry(
        id=make_id(),
        type=EntryType.SUPPRESSION,
        source="nas",
        tags=[],
        created=past,
        expires=past,
        data={"metric": "cpu_pct", "suppress_level": "warning"},
    )
    store.add(TEST_OWNER, entry)
    store._do_cleanup()
    # Entry still in DB — owner hasn't opted in. Use get_entry_by_id
    # since count_active_suppressions filters out expired entries.
    assert store.get_entry_by_id(TEST_OWNER, entry.id) is not None


def test_knowledge_includes_patterns_context_preferences(store):
    now = now_utc()
    for t in (EntryType.PATTERN, EntryType.CONTEXT, EntryType.PREFERENCE):
        store.add(
            TEST_OWNER,
            Entry(
                id=make_id(),
                type=t,
                source="s",
                tags=["t"],
                created=now,
                expires=None,
                data={},
            ),
        )
    assert len(store.get_knowledge(TEST_OWNER)) == 3


def test_knowledge_filtered_by_tags(store):
    now = now_utc()
    store.add(
        TEST_OWNER,
        Entry(
            id=make_id(),
            type=EntryType.PATTERN,
            source="s",
            tags=["infra"],
            created=now,
            expires=None,
            data={},
        ),
    )
    store.add(
        TEST_OWNER,
        Entry(
            id=make_id(),
            type=EntryType.CONTEXT,
            source="s",
            tags=["calendar"],
            created=now,
            expires=None,
            data={},
        ),
    )
    assert len(store.get_knowledge(TEST_OWNER, tags=["infra"])) == 1
    assert len(store.get_knowledge(TEST_OWNER, tags=["calendar"])) == 1


def test_upsert_preference_deduplicates(store):
    entry1 = store.upsert_preference(
        TEST_OWNER,
        key="alert_verbosity",
        scope="global",
        tags=["prefs"],
        data={"key": "alert_verbosity", "value": "verbose", "scope": "global"},
    )
    entry2 = store.upsert_preference(
        TEST_OWNER,
        key="alert_verbosity",
        scope="global",
        tags=["prefs"],
        data={"key": "alert_verbosity", "value": "one_sentence", "scope": "global"},
    )
    prefs = store.get_entries(TEST_OWNER, entry_type=EntryType.PREFERENCE)
    assert len(prefs) == 1
    assert prefs[0].data["value"] == "one_sentence"
    assert entry1.id == entry2.id


def test_get_entries_by_ids_empty_list(store):
    """Calling get_entries_by_ids with an empty list returns [] immediately."""
    assert store.get_entries_by_ids(TEST_OWNER, []) == []


def test_soft_delete_by_id(store):
    now = now_utc()
    entry = Entry(
        id=make_id(),
        type=EntryType.PATTERN,
        source="nas",
        tags=["infra"],
        created=now,
        expires=None,
        data={"description": "test pattern"},
    )
    store.add(TEST_OWNER, entry)
    assert len(store.get_patterns(TEST_OWNER)) == 1
    assert store.soft_delete_by_id(TEST_OWNER, entry.id) is True
    assert len(store.get_patterns(TEST_OWNER)) == 0
    assert len(store.get_deleted(TEST_OWNER)) == 1


def test_soft_delete_by_id_not_found(store):
    assert store.soft_delete_by_id(TEST_OWNER, "nonexistent-id") is False


def test_soft_delete_by_source(store):
    now = now_utc()
    for i in range(3):
        store.add(
            TEST_OWNER,
            Entry(
                id=make_id(),
                type=EntryType.PATTERN,
                source="nas",
                tags=[],
                created=now,
                expires=None,
                data={"description": f"pattern {i}"},
            ),
        )
    store.add(
        TEST_OWNER,
        Entry(
            id=make_id(),
            type=EntryType.PATTERN,
            source="ci",
            tags=[],
            created=now,
            expires=None,
            data={"description": "ci pattern"},
        ),
    )
    trashed = store.soft_delete_by_source(TEST_OWNER, "nas")
    assert trashed == 3
    assert len(store.get_patterns(TEST_OWNER, "nas")) == 0
    assert len(store.get_patterns(TEST_OWNER, "ci")) == 1
    assert len(store.get_deleted(TEST_OWNER)) == 3


def test_soft_delete_by_source_with_type_filter(store):
    now = now_utc()
    store.add(
        TEST_OWNER,
        Entry(
            id=make_id(),
            type=EntryType.PATTERN,
            source="nas",
            tags=[],
            created=now,
            expires=None,
            data={"description": "pattern"},
        ),
    )
    store.upsert_status(TEST_OWNER, "nas", ["infra"], {"metrics": {"cpu": 50}, "ttl_sec": 120})
    trashed = store.soft_delete_by_source(TEST_OWNER, "nas", EntryType.PATTERN)
    assert trashed == 1
    assert store.get_latest_status(TEST_OWNER, "nas") is not None


def test_restore_by_id(store):
    now = now_utc()
    entry = Entry(
        id=make_id(),
        type=EntryType.PATTERN,
        source="nas",
        tags=["infra"],
        created=now,
        expires=None,
        data={"description": "restorable"},
    )
    store.add(TEST_OWNER, entry)
    store.soft_delete_by_id(TEST_OWNER, entry.id)
    assert len(store.get_patterns(TEST_OWNER)) == 0
    assert store.restore_by_id(TEST_OWNER, entry.id) is True
    assert len(store.get_patterns(TEST_OWNER)) == 1
    assert store.get_patterns(TEST_OWNER)[0].data["description"] == "restorable"


def test_restore_not_found(store):
    assert store.restore_by_id(TEST_OWNER, "nonexistent") is False


def test_soft_deleted_entries_auto_expire(store):
    """Soft-deleted entries get expires and are purged by cleanup (opted in)."""
    _opt_in_cleanup(store, TEST_OWNER)
    now = now_utc()
    entry = Entry(
        id=make_id(),
        type=EntryType.PATTERN,
        source="nas",
        tags=[],
        created=now,
        expires=None,
        data={"description": "will expire"},
    )
    store.add(TEST_OWNER, entry)
    store.soft_delete_by_id(TEST_OWNER, entry.id)
    assert len(store.get_deleted(TEST_OWNER)) == 1
    # Backdate the expires timestamp to trigger cleanup
    with store._pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE entries SET expires = %s WHERE id = %s",
            ("2020-01-01T00:00:00+00:00", entry.id),
        )
    store._do_cleanup()
    assert len(store.get_deleted(TEST_OWNER)) == 0


def test_double_soft_delete_is_noop(store):
    now = now_utc()
    entry = Entry(
        id=make_id(),
        type=EntryType.PATTERN,
        source="nas",
        tags=[],
        created=now,
        expires=None,
        data={"description": "test"},
    )
    store.add(TEST_OWNER, entry)
    assert store.soft_delete_by_id(TEST_OWNER, entry.id) is True
    assert store.soft_delete_by_id(TEST_OWNER, entry.id) is False


def test_clear(store):
    store.upsert_status(TEST_OWNER, "nas", [], {"metrics": {}, "ttl_sec": 60})
    store.clear(TEST_OWNER)
    assert store.get_sources(TEST_OWNER) == []


def test_clear_isolates_owners(store):
    """clear(owner_id) must only delete that owner's data."""
    other_owner = "other-owner"
    store.upsert_status(TEST_OWNER, "nas", [], {"metrics": {}, "ttl_sec": 60})
    store.upsert_status(other_owner, "nas", [], {"metrics": {}, "ttl_sec": 60})
    store.clear(TEST_OWNER)
    # TEST_OWNER data gone
    assert store.get_sources(TEST_OWNER) == []
    # Other owner's data intact
    assert store.get_sources(other_owner) == ["nas"]
    # Clean up other owner
    store.clear(other_owner)


def test_cleanup_only_affects_opted_in_owners(store):
    """_do_cleanup() only deletes expired entries for opted-in owners."""
    from datetime import datetime, timedelta, timezone

    other_owner = "other-owner"
    past = datetime.now(timezone.utc) - timedelta(hours=1)
    for owner in (TEST_OWNER, other_owner):
        entry = Entry(
            id=make_id(),
            type=EntryType.SUPPRESSION,
            source="test",
            tags=[],
            created=past,
            expires=past,
            data={"metric": "cpu", "suppress_level": "warning"},
        )
        store.add(owner, entry)
    # Only opt in TEST_OWNER
    _opt_in_cleanup(store, TEST_OWNER)
    # Track entry IDs to verify presence/absence after cleanup
    test_entries = store.get_entries(TEST_OWNER, entry_type=EntryType.SUPPRESSION)
    other_entries = store.get_entries(other_owner, entry_type=EntryType.SUPPRESSION)
    store._do_cleanup()
    # TEST_OWNER's expired entries purged
    assert store.get_entry_by_id(TEST_OWNER, test_entries[0].id) is None
    # other_owner's expired entries retained (not opted in)
    assert store.get_entry_by_id(other_owner, other_entries[0].id) is not None


# ------------------------------------------------------------------
# Pagination tests
# ------------------------------------------------------------------


def test_get_knowledge_pagination(store):
    """Limit and offset work on get_knowledge."""
    for i in range(5):
        store.add(
            TEST_OWNER,
            Entry(
                id=make_id(),
                type=EntryType.NOTE,
                source="test",
                tags=[],
                created=now_utc(),
                expires=None,
                data={"description": f"note-{i}"},
            ),
        )
    all_entries = store.get_knowledge(TEST_OWNER)
    assert len(all_entries) == 5
    page = store.get_knowledge(TEST_OWNER, limit=2)
    assert len(page) == 2
    page2 = store.get_knowledge(TEST_OWNER, limit=2, offset=2)
    assert len(page2) == 2


def test_get_active_alerts_pagination(store):
    """Limit and offset work on get_active_alerts."""
    for i in range(4):
        store.upsert_alert(
            TEST_OWNER,
            "src",
            ["t"],
            f"a{i}",
            {"alert_id": f"a{i}", "level": "warning", "message": f"m{i}", "resolved": False},
        )
    assert len(store.get_active_alerts(TEST_OWNER)) == 4
    page = store.get_active_alerts(TEST_OWNER, limit=2)
    assert len(page) == 2
    page2 = store.get_active_alerts(TEST_OWNER, limit=2, offset=2)
    assert len(page2) == 2


def test_get_entries_pagination(store):
    """Limit and offset work on get_entries."""
    for i in range(3):
        store.add(
            TEST_OWNER,
            Entry(
                id=make_id(),
                type=EntryType.NOTE,
                source="test",
                tags=[],
                created=now_utc(),
                expires=None,
                data={"description": f"note-{i}"},
            ),
        )
    assert len(store.get_entries(TEST_OWNER, limit=2)) == 2
    assert len(store.get_entries(TEST_OWNER, limit=10, offset=2)) == 1


def test_get_deleted_pagination(store):
    """Limit and offset work on get_deleted."""
    for i in range(3):
        entry = store.add(
            TEST_OWNER,
            Entry(
                id=make_id(),
                type=EntryType.NOTE,
                source="test",
                tags=[],
                created=now_utc(),
                expires=None,
                data={"description": f"note-{i}"},
            ),
        )
        store.soft_delete_by_id(TEST_OWNER, entry.id)
    assert len(store.get_deleted(TEST_OWNER)) == 3
    assert len(store.get_deleted(TEST_OWNER, limit=2)) == 2
    assert len(store.get_deleted(TEST_OWNER, limit=2, offset=2)) == 1


# ------------------------------------------------------------------
# Cleanup error logging
# ------------------------------------------------------------------


def test_do_cleanup_logs_errors(store, caplog):
    """_do_cleanup logs error instead of silently swallowing."""
    import logging
    from unittest.mock import patch

    # Mock pool.connection to raise an error
    with (
        caplog.at_level(logging.ERROR, logger="mcp_awareness.postgres_store"),
        patch.object(store._pool, "connection", side_effect=Exception("test error")),
    ):
        store._do_cleanup()
    assert "Cleanup failed: Exception: test error" in caplog.text


# ------------------------------------------------------------------
# SQL-level upsert lookups
# ------------------------------------------------------------------


def test_upsert_alert_concurrent_no_duplicates(store):
    """Concurrent upsert_alert calls for the same alert_id must not create duplicates."""

    def do_upsert(i):
        store.upsert_alert(
            TEST_OWNER,
            "nas",
            ["infra"],
            "cpu-race",
            {
                "alert_id": "cpu-race",
                "level": "warning",
                "message": f"CPU high (attempt {i})",
                "resolved": False,
            },
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        futures = [executor.submit(do_upsert, i) for i in range(4)]
        concurrent.futures.wait(futures)
        for f in futures:
            f.result()  # raise any exceptions

    alerts = store.get_active_alerts(TEST_OWNER, source="nas")
    cpu_race = [a for a in alerts if a.data.get("alert_id") == "cpu-race"]
    assert len(cpu_race) == 1, f"Expected 1 alert but found {len(cpu_race)}"


def test_upsert_alert_different_sources_same_alert_id(store):
    """Two sources can have alerts with the same alert_id independently."""
    store.upsert_alert(
        TEST_OWNER,
        "src-a",
        ["t"],
        "dup-id",
        {"alert_id": "dup-id", "level": "warning", "message": "A", "resolved": False},
    )
    store.upsert_alert(
        TEST_OWNER,
        "src-b",
        ["t"],
        "dup-id",
        {"alert_id": "dup-id", "level": "critical", "message": "B", "resolved": False},
    )
    alerts = store.get_active_alerts(TEST_OWNER)
    assert len(alerts) == 2


def test_upsert_preference_updates_existing(store):
    """upsert_preference updates in place via SQL lookup."""
    store.upsert_preference(
        TEST_OWNER,
        "theme",
        "global",
        ["ui"],
        {"key": "theme", "scope": "global", "value": "dark"},
    )
    store.upsert_preference(
        TEST_OWNER,
        "theme",
        "global",
        ["ui"],
        {"key": "theme", "scope": "global", "value": "light"},
    )
    prefs = store.get_entries(TEST_OWNER, entry_type=EntryType.PREFERENCE)
    assert len(prefs) == 1
    assert prefs[0].data["value"] == "light"


def test_upsert_preference_concurrent_no_duplicates(store):
    """Concurrent upsert_preference calls for the same key+scope must not create duplicates."""

    def do_upsert(i):
        store.upsert_preference(
            TEST_OWNER,
            "theme",
            "global",
            ["ui"],
            {"key": "theme", "scope": "global", "value": f"dark-{i}"},
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        futures = [executor.submit(do_upsert, i) for i in range(4)]
        concurrent.futures.wait(futures)
        for f in futures:
            f.result()

    prefs = store.get_knowledge(TEST_OWNER, entry_type=EntryType.PREFERENCE)
    theme_prefs = [p for p in prefs if p.data.get("key") == "theme"]
    assert len(theme_prefs) == 1, f"Expected 1 preference but found {len(theme_prefs)}"


# ------------------------------------------------------------------
# Delete by tags
# ------------------------------------------------------------------


def test_soft_delete_by_tags_and_logic(store):
    """Only entries matching ALL tags are deleted."""
    store.add(
        TEST_OWNER,
        Entry(
            id=make_id(),
            type=EntryType.NOTE,
            source="test",
            tags=["qa", "project"],
            created=now_utc(),
            expires=None,
            data={"description": "both tags"},
        ),
    )
    store.add(
        TEST_OWNER,
        Entry(
            id=make_id(),
            type=EntryType.NOTE,
            source="test",
            tags=["qa"],
            created=now_utc(),
            expires=None,
            data={"description": "only qa"},
        ),
    )
    store.add(
        TEST_OWNER,
        Entry(
            id=make_id(),
            type=EntryType.NOTE,
            source="test",
            tags=["project"],
            created=now_utc(),
            expires=None,
            data={"description": "only project"},
        ),
    )
    count = store.soft_delete_by_tags(TEST_OWNER, ["qa", "project"])
    assert count == 1
    remaining = store.get_entries(TEST_OWNER)
    assert len(remaining) == 2
    descs = {e.data["description"] for e in remaining}
    assert descs == {"only qa", "only project"}


def test_soft_delete_by_tags_empty(store):
    """Empty tag list deletes nothing."""
    store.add(
        TEST_OWNER,
        Entry(
            id=make_id(),
            type=EntryType.NOTE,
            source="test",
            tags=["qa"],
            created=now_utc(),
            expires=None,
            data={"description": "test"},
        ),
    )
    assert store.soft_delete_by_tags(TEST_OWNER, []) == 0
    assert len(store.get_entries(TEST_OWNER)) == 1


def test_soft_delete_by_tags_single(store):
    """Single tag matches all entries with that tag."""
    for i in range(3):
        store.add(
            TEST_OWNER,
            Entry(
                id=make_id(),
                type=EntryType.NOTE,
                source="test",
                tags=["qa-test"] if i < 2 else ["other"],
                created=now_utc(),
                expires=None,
                data={"description": f"note-{i}"},
            ),
        )
    count = store.soft_delete_by_tags(TEST_OWNER, ["qa-test"])
    assert count == 2
    assert len(store.get_entries(TEST_OWNER)) == 1


# ------------------------------------------------------------------
# Restore by tags
# ------------------------------------------------------------------


def test_restore_by_tags(store):
    """Restore trashed entries matching ALL tags."""
    for i in range(3):
        entry = store.add(
            TEST_OWNER,
            Entry(
                id=make_id(),
                type=EntryType.NOTE,
                source="test",
                tags=["qa-test", "batch"] if i < 2 else ["other"],
                created=now_utc(),
                expires=None,
                data={"description": f"note-{i}"},
            ),
        )
        store.soft_delete_by_id(TEST_OWNER, entry.id)
    assert len(store.get_deleted(TEST_OWNER)) == 3
    restored = store.restore_by_tags(TEST_OWNER, ["qa-test", "batch"])
    assert restored == 2
    assert len(store.get_entries(TEST_OWNER)) == 2
    assert len(store.get_deleted(TEST_OWNER)) == 1


def test_restore_by_tags_empty(store):
    """Empty tag list restores nothing."""
    assert store.restore_by_tags(TEST_OWNER, []) == 0


# ------------------------------------------------------------------
# Since filter tests
# ------------------------------------------------------------------


def test_get_knowledge_since(store):
    """since param filters by updated timestamp."""
    from datetime import datetime, timedelta, timezone

    old = datetime.now(timezone.utc) - timedelta(hours=2)
    recent = datetime.now(timezone.utc) - timedelta(minutes=5)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=1)

    store.add(
        TEST_OWNER,
        Entry(
            id=make_id(),
            type=EntryType.NOTE,
            source="test",
            tags=[],
            created=old,
            expires=None,
            data={"description": "old note"},
        ),
    )
    store.add(
        TEST_OWNER,
        Entry(
            id=make_id(),
            type=EntryType.NOTE,
            source="test",
            tags=[],
            created=recent,
            expires=None,
            data={"description": "recent note"},
        ),
    )
    all_entries = store.get_knowledge(TEST_OWNER)
    assert len(all_entries) == 2
    filtered = store.get_knowledge(TEST_OWNER, since=cutoff)
    assert len(filtered) == 1
    assert filtered[0].data["description"] == "recent note"


def test_get_entries_since(store):
    """since param works on get_entries."""
    from datetime import datetime, timedelta, timezone

    old = datetime.now(timezone.utc) - timedelta(hours=2)
    recent = datetime.now(timezone.utc)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=1)

    store.add(
        TEST_OWNER,
        Entry(
            id=make_id(),
            type=EntryType.NOTE,
            source="test",
            tags=[],
            created=old,
            expires=None,
            data={"description": "old"},
        ),
    )
    store.add(
        TEST_OWNER,
        Entry(
            id=make_id(),
            type=EntryType.NOTE,
            source="test",
            tags=[],
            created=recent,
            expires=None,
            data={"description": "new"},
        ),
    )
    assert len(store.get_entries(TEST_OWNER, since=cutoff)) == 1


def test_get_active_alerts_since(store):
    """since param works on get_active_alerts."""
    from datetime import datetime, timedelta, timezone

    old = datetime.now(timezone.utc) - timedelta(hours=2)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=1)

    store.upsert_alert(
        TEST_OWNER,
        "src",
        ["t"],
        "old-alert",
        {"alert_id": "old-alert", "level": "warning", "message": "old", "resolved": False},
    )
    # Backdate the alert
    with store._pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE entries SET updated = %s WHERE data->>'alert_id' = %s",
            (old, "old-alert"),
        )

    store.upsert_alert(
        TEST_OWNER,
        "src",
        ["t"],
        "new-alert",
        {"alert_id": "new-alert", "level": "warning", "message": "new", "resolved": False},
    )
    assert len(store.get_active_alerts(TEST_OWNER)) == 2
    assert len(store.get_active_alerts(TEST_OWNER, since=cutoff)) == 1


def test_get_deleted_since(store):
    """since param works on get_deleted."""
    from datetime import datetime, timedelta, timezone

    old = datetime.now(timezone.utc) - timedelta(hours=2)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=1)

    entry1 = store.add(
        TEST_OWNER,
        Entry(
            id=make_id(),
            type=EntryType.NOTE,
            source="test",
            tags=[],
            created=now_utc(),
            expires=None,
            data={"description": "old delete"},
        ),
    )
    store.soft_delete_by_id(TEST_OWNER, entry1.id)
    # Backdate the deletion
    with store._pool.connection() as conn, conn.cursor() as cur:
        cur.execute("UPDATE entries SET deleted = %s WHERE id = %s", (old, entry1.id))

    entry2 = store.add(
        TEST_OWNER,
        Entry(
            id=make_id(),
            type=EntryType.NOTE,
            source="test",
            tags=[],
            created=now_utc(),
            expires=None,
            data={"description": "recent delete"},
        ),
    )
    store.soft_delete_by_id(TEST_OWNER, entry2.id)

    assert len(store.get_deleted(TEST_OWNER)) == 2
    assert len(store.get_deleted(TEST_OWNER, since=cutoff)) == 1


# ------------------------------------------------------------------
# Read / action tracking tests
# ------------------------------------------------------------------


def test_log_read_and_get_reads(store):
    """log_read records reads, get_reads retrieves them."""
    entry = store.add(
        TEST_OWNER,
        Entry(
            id=make_id(),
            type=EntryType.NOTE,
            source="test",
            tags=["demo"],
            created=now_utc(),
            expires=None,
            data={"description": "readable"},
        ),
    )
    store.log_read(TEST_OWNER, [entry.id], tool_used="get_knowledge")
    store.log_read(TEST_OWNER, [entry.id], tool_used="get_knowledge", platform="claude-code")
    reads = store.get_reads(TEST_OWNER, entry_id=entry.id)
    assert len(reads) == 2
    assert reads[0]["entry_id"] == entry.id
    assert reads[0]["tool_used"] == "get_knowledge"


def test_log_read_empty_list(store):
    """log_read with empty list is a no-op."""
    store.log_read(TEST_OWNER, [], tool_used="test")
    assert store.get_reads(TEST_OWNER) == []


def test_log_action_and_get_actions(store):
    """log_action records actions, get_actions retrieves them."""
    entry = store.add(
        TEST_OWNER,
        Entry(
            id=make_id(),
            type=EntryType.NOTE,
            source="test",
            tags=["project", "mcp-awareness"],
            created=now_utc(),
            expires=None,
            data={"description": "actionable"},
        ),
    )
    result = store.log_action(
        TEST_OWNER,
        entry_id=entry.id,
        action="created GitHub issue #42",
        platform="claude-code",
        detail="https://github.com/cmeans/mcp-awareness/issues/42",
    )
    assert result["action"] == "created GitHub issue #42"
    assert result["tags"] == ["project", "mcp-awareness"]  # copied from entry

    actions = store.get_actions(TEST_OWNER, entry_id=entry.id)
    assert len(actions) == 1
    assert actions[0]["action"] == "created GitHub issue #42"
    assert actions[0]["detail"] == "https://github.com/cmeans/mcp-awareness/issues/42"


def test_log_action_custom_tags(store):
    """log_action accepts custom tags instead of copying from entry."""
    entry = store.add(
        TEST_OWNER,
        Entry(
            id=make_id(),
            type=EntryType.NOTE,
            source="test",
            tags=["original"],
            created=now_utc(),
            expires=None,
            data={"description": "test"},
        ),
    )
    result = store.log_action(TEST_OWNER, entry_id=entry.id, action="test", tags=["custom", "tags"])
    assert result["tags"] == ["custom", "tags"]


def test_log_action_invalid_entry_id(store):
    """log_action returns error for nonexistent entry_id."""
    result = store.log_action(TEST_OWNER, entry_id="nonexistent-id", action="test")
    assert result["status"] == "error"
    assert "not found" in result["message"].lower()


def test_log_read_records_reads(store):
    """log_read inserts read records retrievable via get_reads."""
    now = now_utc()
    entry = store.add(
        TEST_OWNER,
        Entry(
            id=make_id(),
            type=EntryType.NOTE,
            source="test",
            tags=["read-test"],
            created=now,
            expires=None,
            data={"description": "read tracking test"},
        ),
    )
    store.log_read(TEST_OWNER, [entry.id], tool_used="get_knowledge", platform="claude-code")
    reads = store.get_reads(TEST_OWNER, entry_id=entry.id)
    assert len(reads) >= 1
    assert reads[0]["entry_id"] == entry.id
    assert reads[0]["tool_used"] == "get_knowledge"
    assert reads[0]["platform"] == "claude-code"


def test_log_read_empty_list_is_noop(store):
    """log_read with empty list returns immediately without touching the DB."""
    store.log_read(TEST_OWNER, [], tool_used="get_knowledge")  # should not raise


def test_log_read_silences_pool_errors(store):
    """log_read swallows exceptions when the pool is broken (fire-and-forget)."""
    from unittest.mock import patch

    now = now_utc()
    entry = store.add(
        TEST_OWNER,
        Entry(
            id=make_id(),
            type=EntryType.NOTE,
            source="test",
            tags=["read-test"],
            created=now,
            expires=None,
            data={"description": "pool failure test"},
        ),
    )
    with patch.object(store._pool, "connection", side_effect=RuntimeError("pool closed")):
        # Should not raise despite the pool being broken
        store.log_read(TEST_OWNER, [entry.id], tool_used="get_knowledge")


def test_get_actions_filter_by_tags(store):
    """get_actions can filter by tags."""
    entry = store.add(
        TEST_OWNER,
        Entry(
            id=make_id(),
            type=EntryType.NOTE,
            source="test",
            tags=["project"],
            created=now_utc(),
            expires=None,
            data={"description": "test"},
        ),
    )
    store.log_action(
        TEST_OWNER, entry_id=entry.id, action="tagged action", tags=["project", "deploy"]
    )
    store.log_action(TEST_OWNER, entry_id=entry.id, action="other action", tags=["personal"])

    project_actions = store.get_actions(TEST_OWNER, tags=["project"])
    assert len(project_actions) == 1
    assert project_actions[0]["action"] == "tagged action"


def test_get_unread(store):
    """get_unread returns entries with zero reads."""
    e1 = store.add(
        TEST_OWNER,
        Entry(
            id=make_id(),
            type=EntryType.NOTE,
            source="test",
            tags=[],
            created=now_utc(),
            expires=None,
            data={"description": "read entry"},
        ),
    )
    store.add(
        TEST_OWNER,
        Entry(
            id=make_id(),
            type=EntryType.NOTE,
            source="test",
            tags=[],
            created=now_utc(),
            expires=None,
            data={"description": "unread entry"},
        ),
    )
    store.log_read(TEST_OWNER, [e1.id], tool_used="test")
    unread = store.get_unread(TEST_OWNER)
    assert len(unread) == 1
    assert unread[0].data["description"] == "unread entry"


def test_get_activity(store):
    """get_activity returns combined reads + actions feed."""
    entry = store.add(
        TEST_OWNER,
        Entry(
            id=make_id(),
            type=EntryType.NOTE,
            source="test",
            tags=[],
            created=now_utc(),
            expires=None,
            data={"description": "test"},
        ),
    )
    store.log_read(TEST_OWNER, [entry.id], tool_used="get_knowledge")
    store.log_action(TEST_OWNER, entry_id=entry.id, action="used for context")

    activity = store.get_activity(TEST_OWNER)
    assert len(activity) == 2
    types = {a["event_type"] for a in activity}
    assert types == {"read", "action"}


def test_get_read_counts(store):
    """get_read_counts returns counts and last_read per entry."""
    e1 = store.add(
        TEST_OWNER,
        Entry(
            id=make_id(),
            type=EntryType.NOTE,
            source="test",
            tags=[],
            created=now_utc(),
            expires=None,
            data={"description": "popular"},
        ),
    )
    e2 = store.add(
        TEST_OWNER,
        Entry(
            id=make_id(),
            type=EntryType.NOTE,
            source="test",
            tags=[],
            created=now_utc(),
            expires=None,
            data={"description": "unpopular"},
        ),
    )
    store.log_read(TEST_OWNER, [e1.id], tool_used="test")
    store.log_read(TEST_OWNER, [e1.id], tool_used="test")
    store.log_read(TEST_OWNER, [e1.id], tool_used="test")

    counts = store.get_read_counts(TEST_OWNER, [e1.id, e2.id])
    assert counts[e1.id]["read_count"] == 3
    assert counts[e1.id]["last_read"] is not None
    assert e2.id not in counts  # no reads


def test_clear_removes_reads_and_actions(store):
    """clear() removes reads and actions along with entries."""
    entry = store.add(
        TEST_OWNER,
        Entry(
            id=make_id(),
            type=EntryType.NOTE,
            source="test",
            tags=[],
            created=now_utc(),
            expires=None,
            data={"description": "test"},
        ),
    )
    store.log_read(TEST_OWNER, [entry.id], tool_used="test")
    store.log_action(TEST_OWNER, entry_id=entry.id, action="test")
    store.clear(TEST_OWNER)
    assert store.get_reads(TEST_OWNER) == []
    assert store.get_actions(TEST_OWNER) == []


# ------------------------------------------------------------------
# Intention tests
# ------------------------------------------------------------------


def test_create_and_get_intention(store):
    """Create an intention and retrieve it."""
    now = now_utc()
    entry = Entry(
        id=make_id(),
        type=EntryType.INTENTION,
        source="personal",
        tags=["errands"],
        created=now,
        expires=None,
        data={
            "goal": "Pick up milk",
            "state": "pending",
            "deliver_at": None,
            "constraints": "organic, oat-preferred",
            "urgency": "normal",
            "recurrence": None,
            "learned_from": "test",
        },
    )
    store.add(TEST_OWNER, entry)
    intentions = store.get_intentions(TEST_OWNER)
    assert len(intentions) == 1
    assert intentions[0].data["goal"] == "Pick up milk"
    assert intentions[0].data["state"] == "pending"


def test_get_intentions_filter_by_state(store):
    """get_intentions filters by state."""
    now = now_utc()
    for state in ("pending", "pending", "fired", "completed"):
        store.add(
            TEST_OWNER,
            Entry(
                id=make_id(),
                type=EntryType.INTENTION,
                source="test",
                tags=[],
                created=now,
                expires=None,
                data={"goal": f"goal-{state}", "state": state},
            ),
        )
    assert len(store.get_intentions(TEST_OWNER, state="pending")) == 2
    assert len(store.get_intentions(TEST_OWNER, state="fired")) == 1
    assert len(store.get_intentions(TEST_OWNER, state="completed")) == 1
    assert len(store.get_intentions(TEST_OWNER)) == 4


def test_get_intentions_filter_by_tags(store):
    """get_intentions filters by tags."""
    now = now_utc()
    store.add(
        TEST_OWNER,
        Entry(
            id=make_id(),
            type=EntryType.INTENTION,
            source="test",
            tags=["errands", "groceries"],
            created=now,
            expires=None,
            data={"goal": "buy milk", "state": "pending"},
        ),
    )
    store.add(
        TEST_OWNER,
        Entry(
            id=make_id(),
            type=EntryType.INTENTION,
            source="test",
            tags=["work"],
            created=now,
            expires=None,
            data={"goal": "file report", "state": "pending"},
        ),
    )
    assert len(store.get_intentions(TEST_OWNER, tags=["groceries"])) == 1
    assert len(store.get_intentions(TEST_OWNER, tags=["work"])) == 1


def test_get_intentions_filter_by_source_and_limit(store):
    """get_intentions filters by source and respects limit."""
    now = now_utc()
    for i in range(3):
        store.add(
            TEST_OWNER,
            Entry(
                id=make_id(),
                type=EntryType.INTENTION,
                source="personal",
                tags=[],
                created=now,
                expires=None,
                data={"goal": f"personal-{i}", "state": "pending"},
            ),
        )
    store.add(
        TEST_OWNER,
        Entry(
            id=make_id(),
            type=EntryType.INTENTION,
            source="work",
            tags=[],
            created=now,
            expires=None,
            data={"goal": "work goal", "state": "pending"},
        ),
    )
    assert len(store.get_intentions(TEST_OWNER, source="personal")) == 3
    assert len(store.get_intentions(TEST_OWNER, source="work")) == 1
    assert len(store.get_intentions(TEST_OWNER, limit=2)) == 2


def test_update_intention_state(store):
    """update_intention_state transitions state and records changelog."""
    now = now_utc()
    entry = Entry(
        id=make_id(),
        type=EntryType.INTENTION,
        source="test",
        tags=[],
        created=now,
        expires=None,
        data={"goal": "test goal", "state": "pending"},
    )
    store.add(TEST_OWNER, entry)
    result = store.update_intention_state(
        TEST_OWNER, entry.id, "fired", reason="time-based trigger"
    )
    assert result is not None
    assert result.data["state"] == "fired"
    assert result.data["state_reason"] == "time-based trigger"
    assert len(result.data["changelog"]) == 1
    assert result.data["changelog"][0]["changed"]["state"] == "pending"


def test_update_intention_state_not_found(store):
    """update_intention_state returns None for nonexistent entry."""
    assert store.update_intention_state(TEST_OWNER, "nonexistent", "fired") is None


def test_update_intention_state_wrong_type(store):
    """update_intention_state returns None for non-intention entries."""
    now = now_utc()
    entry = Entry(
        id=make_id(),
        type=EntryType.NOTE,
        source="test",
        tags=[],
        created=now,
        expires=None,
        data={"description": "not an intention"},
    )
    store.add(TEST_OWNER, entry)
    assert store.update_intention_state(TEST_OWNER, entry.id, "fired") is None


def test_update_intention_state_wrong_owner(store):
    """update_intention_state rejects updates from a different owner."""
    now = now_utc()
    entry = Entry(
        id=make_id(),
        type=EntryType.INTENTION,
        source="test",
        tags=[],
        created=now,
        expires=None,
        data={"goal": "test goal", "state": "pending"},
    )
    store.add(TEST_OWNER, entry)
    result = store.update_intention_state("wrong-owner", entry.id, "fired")
    assert result is None


def test_get_fired_intentions(store):
    """get_fired_intentions returns pending intentions with past deliver_at."""
    from datetime import timedelta

    now = now_utc()
    past = now - timedelta(hours=1)
    future = now + timedelta(hours=1)

    # Past deliver_at — should fire
    store.add(
        TEST_OWNER,
        Entry(
            id=make_id(),
            type=EntryType.INTENTION,
            source="test",
            tags=[],
            created=now,
            expires=None,
            data={"goal": "overdue", "state": "pending", "deliver_at": past.isoformat()},
        ),
    )
    # Future deliver_at — should not fire
    store.add(
        TEST_OWNER,
        Entry(
            id=make_id(),
            type=EntryType.INTENTION,
            source="test",
            tags=[],
            created=now,
            expires=None,
            data={"goal": "not yet", "state": "pending", "deliver_at": future.isoformat()},
        ),
    )
    # No deliver_at — should not fire
    store.add(
        TEST_OWNER,
        Entry(
            id=make_id(),
            type=EntryType.INTENTION,
            source="test",
            tags=[],
            created=now,
            expires=None,
            data={"goal": "no time", "state": "pending", "deliver_at": None},
        ),
    )
    # Already fired — should not appear
    store.add(
        TEST_OWNER,
        Entry(
            id=make_id(),
            type=EntryType.INTENTION,
            source="test",
            tags=[],
            created=now,
            expires=None,
            data={"goal": "already done", "state": "fired", "deliver_at": past.isoformat()},
        ),
    )

    fired = store.get_fired_intentions(TEST_OWNER)
    assert len(fired) == 1
    assert fired[0].data["goal"] == "overdue"


# ------------------------------------------------------------------
# SQL-level improvements
# ------------------------------------------------------------------


def test_fired_intentions_sql_filters_future_deliver_at(store):
    """Verify future deliver_at intentions are excluded at the SQL level."""
    from datetime import timedelta

    now = now_utc()
    future = now + timedelta(hours=24)

    # Create only a future intention — nothing should come back
    store.add(
        TEST_OWNER,
        Entry(
            id=make_id(),
            type=EntryType.INTENTION,
            source="test",
            tags=[],
            created=now,
            expires=None,
            data={
                "goal": "future task",
                "state": "pending",
                "deliver_at": future.isoformat(),
            },
        ),
    )

    fired = store.get_fired_intentions(TEST_OWNER)
    assert fired == [], f"Expected no fired intentions for future deliver_at, got {len(fired)}"


def test_get_knowledge_default_sort_desc(store):
    """get_knowledge returns most recent entries first (updated DESC)."""
    from datetime import timedelta

    now = now_utc()
    old = now - timedelta(hours=2)
    store.add(
        TEST_OWNER,
        Entry(
            id=make_id(),
            type=EntryType.NOTE,
            source="test",
            tags=[],
            created=old,
            expires=None,
            data={"description": "old note"},
        ),
    )
    store.add(
        TEST_OWNER,
        Entry(
            id=make_id(),
            type=EntryType.NOTE,
            source="test",
            tags=[],
            created=now,
            expires=None,
            data={"description": "recent note"},
        ),
    )
    entries = store.get_knowledge(TEST_OWNER, limit=1)
    assert len(entries) == 1
    assert entries[0].data["description"] == "recent note"


def test_get_knowledge_until(store):
    """until param filters by updated <= timestamp."""
    from datetime import timedelta

    now = now_utc()
    old = now - timedelta(hours=2)
    cutoff = now - timedelta(hours=1)
    store.add(
        TEST_OWNER,
        Entry(
            id=make_id(),
            type=EntryType.NOTE,
            source="test",
            tags=[],
            created=old,
            expires=None,
            data={"description": "old note"},
        ),
    )
    store.add(
        TEST_OWNER,
        Entry(
            id=make_id(),
            type=EntryType.NOTE,
            source="test",
            tags=[],
            created=now,
            expires=None,
            data={"description": "recent note"},
        ),
    )
    entries = store.get_knowledge(TEST_OWNER, until=cutoff)
    assert len(entries) == 1
    assert entries[0].data["description"] == "old note"


def test_get_knowledge_learned_from(store):
    """learned_from param filters by data->>'learned_from'."""
    now = now_utc()
    store.add(
        TEST_OWNER,
        Entry(
            id=make_id(),
            type=EntryType.NOTE,
            source="test",
            tags=[],
            created=now,
            expires=None,
            data={"description": "from code", "learned_from": "claude-code"},
        ),
    )
    store.add(
        TEST_OWNER,
        Entry(
            id=make_id(),
            type=EntryType.NOTE,
            source="test",
            tags=[],
            created=now,
            expires=None,
            data={"description": "from desktop", "learned_from": "claude.ai"},
        ),
    )
    entries = store.get_knowledge(TEST_OWNER, learned_from="claude-code")
    assert len(entries) == 1
    assert entries[0].data["description"] == "from code"


def test_get_active_alerts_resolved_filtered_in_sql(store):
    """Resolved alerts are filtered in SQL, not post-fetch."""
    store.upsert_alert(
        TEST_OWNER,
        "src",
        ["t"],
        "active",
        {"alert_id": "active", "level": "warning", "message": "active", "resolved": False},
    )
    store.upsert_alert(
        TEST_OWNER,
        "src",
        ["t"],
        "resolved",
        {"alert_id": "resolved", "level": "warning", "message": "resolved", "resolved": True},
    )
    alerts = store.get_active_alerts(TEST_OWNER)
    assert len(alerts) == 1
    assert alerts[0].data["alert_id"] == "active"


def test_sql_pagination_limit_offset(store):
    """LIMIT/OFFSET pushed to SQL — verify with get_entries."""
    for i in range(5):
        store.add(
            TEST_OWNER,
            Entry(
                id=make_id(),
                type=EntryType.NOTE,
                source="test",
                tags=[],
                created=now_utc(),
                expires=None,
                data={"description": f"note-{i}"},
            ),
        )
    page1 = store.get_entries(TEST_OWNER, limit=2)
    assert len(page1) == 2
    page2 = store.get_entries(TEST_OWNER, limit=2, offset=2)
    assert len(page2) == 2
    # Pages shouldn't overlap
    ids1 = {e.id for e in page1}
    ids2 = {e.id for e in page2}
    assert ids1.isdisjoint(ids2)


def test_get_knowledge_include_history_only_with_pagination(store):
    """include_history='only' with limit/offset uses Python-side pagination."""
    now = now_utc()
    for i in range(3):
        entry = Entry(
            id=make_id(),
            type=EntryType.NOTE,
            source="test",
            tags=[],
            created=now,
            expires=None,
            data={"description": f"note-{i}"},
        )
        store.add(TEST_OWNER, entry)
        store.update_entry(TEST_OWNER, entry.id, {"description": f"updated-{i}"})
    all_with_history = store.get_knowledge(TEST_OWNER, include_history="only")
    assert len(all_with_history) == 3
    page = store.get_knowledge(TEST_OWNER, include_history="only", limit=2, offset=1)
    assert len(page) == 2


# ---------------------------------------------------------------------------
# created_after / created_before filters
# ---------------------------------------------------------------------------


def test_get_knowledge_created_after(store):
    """created_after filters by creation time, not update time."""
    from datetime import timedelta

    early = now_utc() - timedelta(hours=2)
    late = now_utc()
    e1 = Entry(
        id=make_id(),
        type=EntryType.NOTE,
        source="test",
        tags=[],
        created=early,
        updated=late,  # updated recently, but created early
        expires=None,
        data={"description": "old note"},
    )
    e2 = Entry(
        id=make_id(),
        type=EntryType.NOTE,
        source="test",
        tags=[],
        created=late,
        expires=None,
        data={"description": "new note"},
    )
    store.add(TEST_OWNER, e1)
    store.add(TEST_OWNER, e2)
    cutoff = now_utc() - timedelta(hours=1)
    results = store.get_knowledge(TEST_OWNER, created_after=cutoff)
    assert len(results) == 1
    assert results[0].data["description"] == "new note"


def test_get_knowledge_created_before(store):
    """created_before filters by creation time."""
    from datetime import timedelta

    early = now_utc() - timedelta(hours=2)
    late = now_utc()
    e1 = Entry(
        id=make_id(),
        type=EntryType.NOTE,
        source="test",
        tags=[],
        created=early,
        expires=None,
        data={"description": "old note"},
    )
    e2 = Entry(
        id=make_id(),
        type=EntryType.NOTE,
        source="test",
        tags=[],
        created=late,
        expires=None,
        data={"description": "new note"},
    )
    store.add(TEST_OWNER, e1)
    store.add(TEST_OWNER, e2)
    cutoff = now_utc() - timedelta(hours=1)
    results = store.get_knowledge(TEST_OWNER, created_before=cutoff)
    assert len(results) == 1
    assert results[0].data["description"] == "old note"


# ---------------------------------------------------------------------------
# Connection pooling
# ---------------------------------------------------------------------------


def test_pool_serves_concurrent_requests(store):
    """Pool handles multiple sequential operations without connection issues."""
    # Rapid-fire operations that would serialize on a single connection
    for i in range(10):
        store.upsert_status(TEST_OWNER, f"src-{i}", ["t"], {"metrics": {}, "ttl_sec": 120})
    sources = store.get_sources(TEST_OWNER)
    assert len(sources) == 10


def test_pool_recovers_from_errors(store):
    """Pool provides working connections even after query errors."""
    # Force an error with invalid SQL via a raw pool connection
    try:
        with store._pool.connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM nonexistent_table_xyz")
    except Exception:
        pass  # Expected — table doesn't exist
    # Pool should still work — next connection is clean
    store.upsert_status(TEST_OWNER, "after-error", ["t"], {"metrics": {}, "ttl_sec": 120})
    assert "after-error" in store.get_sources(TEST_OWNER)


# ---------------------------------------------------------------------------
# Embeddings / semantic search
# ---------------------------------------------------------------------------


class TestEmbeddings:
    """Store-level tests for embedding storage and vector search.

    Uses hand-crafted vectors (unit vectors along axes) so similarity
    ordering is deterministic without a real embedding model.
    """

    @staticmethod
    def _vec(dim: int, axis: int) -> list[float]:
        """Unit vector along a specific axis in `dim`-dimensional space."""
        v = [0.0] * dim
        v[axis] = 1.0
        return v

    def test_upsert_embedding(self, store):
        """Store and retrieve an embedding."""
        now = now_utc()
        entry = Entry(
            id=make_id(),
            type=EntryType.NOTE,
            source="test",
            tags=["a"],
            created=now,
            expires=None,
            data={"description": "test note"},
        )
        store.add(TEST_OWNER, entry)
        vec = self._vec(768, 0)
        store.upsert_embedding(TEST_OWNER, entry.id, "test-model", 768, "abc123", vec)
        # Should be searchable
        results = store.semantic_search(TEST_OWNER, vec, "test-model", limit=5)
        assert len(results) == 1
        found_entry, score = results[0]
        assert found_entry.id == entry.id
        assert score > 0  # RRF score: 1/(60+rnk) summed across branches

    def test_upsert_embedding_replaces(self, store):
        """Upserting same entry+model replaces the embedding."""
        now = now_utc()
        entry = Entry(
            id=make_id(),
            type=EntryType.NOTE,
            source="test",
            tags=[],
            created=now,
            expires=None,
            data={"description": "test"},
        )
        store.add(TEST_OWNER, entry)
        store.upsert_embedding(TEST_OWNER, entry.id, "test-model", 768, "hash1", self._vec(768, 0))
        store.upsert_embedding(TEST_OWNER, entry.id, "test-model", 768, "hash2", self._vec(768, 1))
        # Search along axis 1 should find it
        results = store.semantic_search(TEST_OWNER, self._vec(768, 1), "test-model", limit=5)
        assert len(results) == 1
        assert results[0][1] > 0  # RRF score

    def test_upsert_embedding_preserves_created(self, store):
        """Upserting same entry+model preserves the original created timestamp."""
        import time

        now = now_utc()
        entry = Entry(
            id=make_id(),
            type=EntryType.NOTE,
            source="test",
            tags=[],
            created=now,
            expires=None,
            data={"description": "test"},
        )
        store.add(TEST_OWNER, entry)
        store.upsert_embedding(TEST_OWNER, entry.id, "test-model", 768, "hash1", self._vec(768, 0))

        # Read the created timestamp after the first insert
        with store._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT created FROM embeddings WHERE entry_id = %s AND model = %s",
                (entry.id, "test-model"),
            )
            original_created = cur.fetchone()["created"]

        # Small delay so any now() call would produce a different timestamp
        time.sleep(0.05)

        # Upsert again with a different hash and vector
        store.upsert_embedding(TEST_OWNER, entry.id, "test-model", 768, "hash2", self._vec(768, 1))

        # Read the created timestamp after the second upsert
        with store._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT created, text_hash FROM embeddings WHERE entry_id = %s AND model = %s",
                (entry.id, "test-model"),
            )
            row = cur.fetchone()
            updated_created = row["created"]
            updated_hash = row["text_hash"]

        # created must be preserved from the first insert
        assert updated_created == original_created
        # but the hash should have been updated
        assert updated_hash == "hash2"

    def test_semantic_search_ordering(self, store):
        """Results are ordered by similarity (closest first)."""
        now = now_utc()
        ids = []
        for i in range(3):
            entry = Entry(
                id=make_id(),
                type=EntryType.NOTE,
                source="test",
                tags=[],
                created=now,
                expires=None,
                data={"description": f"note-{i}"},
            )
            store.add(TEST_OWNER, entry)
            store.upsert_embedding(TEST_OWNER, entry.id, "m", 768, f"h{i}", self._vec(768, i))
            ids.append(entry.id)
        # Query along axis 0 — entry 0 should be first
        results = store.semantic_search(TEST_OWNER, self._vec(768, 0), "m", limit=3)
        assert results[0][0].id == ids[0]
        assert results[0][1] > results[1][1]

    def test_semantic_search_with_type_filter(self, store):
        """Type filter narrows results."""
        now = now_utc()
        note = Entry(
            id=make_id(),
            type=EntryType.NOTE,
            source="test",
            tags=[],
            created=now,
            expires=None,
            data={"description": "note"},
        )
        alert = Entry(
            id=make_id(),
            type=EntryType.ALERT,
            source="test",
            tags=[],
            created=now,
            expires=None,
            data={"alert_id": "a1", "message": "alert", "level": "warning", "resolved": False},
        )
        store.add(TEST_OWNER, note)
        store.add(TEST_OWNER, alert)
        vec = self._vec(768, 0)
        store.upsert_embedding(TEST_OWNER, note.id, "m", 768, "h1", vec)
        store.upsert_embedding(TEST_OWNER, alert.id, "m", 768, "h2", vec)
        # Filter to notes only
        results = store.semantic_search(TEST_OWNER, vec, "m", entry_type=EntryType.NOTE)
        assert len(results) == 1
        assert results[0][0].type == EntryType.NOTE

    def test_semantic_search_with_source_filter(self, store):
        """Source filter narrows results."""
        now = now_utc()
        e1 = Entry(
            id=make_id(),
            type=EntryType.NOTE,
            source="nas",
            tags=[],
            created=now,
            expires=None,
            data={"description": "nas note"},
        )
        e2 = Entry(
            id=make_id(),
            type=EntryType.NOTE,
            source="personal",
            tags=[],
            created=now,
            expires=None,
            data={"description": "personal note"},
        )
        store.add(TEST_OWNER, e1)
        store.add(TEST_OWNER, e2)
        vec = self._vec(768, 0)
        store.upsert_embedding(TEST_OWNER, e1.id, "m", 768, "h1", vec)
        store.upsert_embedding(TEST_OWNER, e2.id, "m", 768, "h2", vec)
        results = store.semantic_search(TEST_OWNER, vec, "m", source="nas")
        assert len(results) == 1
        assert results[0][0].source == "nas"

    def test_semantic_search_with_tag_filter(self, store):
        """Tag filter narrows results."""
        now = now_utc()
        e1 = Entry(
            id=make_id(),
            type=EntryType.NOTE,
            source="test",
            tags=["infra"],
            created=now,
            expires=None,
            data={"description": "infra note"},
        )
        e2 = Entry(
            id=make_id(),
            type=EntryType.NOTE,
            source="test",
            tags=["personal"],
            created=now,
            expires=None,
            data={"description": "personal note"},
        )
        store.add(TEST_OWNER, e1)
        store.add(TEST_OWNER, e2)
        vec = self._vec(768, 0)
        store.upsert_embedding(TEST_OWNER, e1.id, "m", 768, "h1", vec)
        store.upsert_embedding(TEST_OWNER, e2.id, "m", 768, "h2", vec)
        results = store.semantic_search(TEST_OWNER, vec, "m", tags=["infra"])
        assert len(results) == 1
        assert "infra" in results[0][0].tags

    def test_semantic_search_excludes_deleted(self, store):
        """Soft-deleted entries are excluded from search."""
        now = now_utc()
        entry = Entry(
            id=make_id(),
            type=EntryType.NOTE,
            source="test",
            tags=[],
            created=now,
            expires=None,
            data={"description": "test"},
        )
        store.add(TEST_OWNER, entry)
        vec = self._vec(768, 0)
        store.upsert_embedding(TEST_OWNER, entry.id, "m", 768, "h1", vec)
        store.soft_delete_by_id(TEST_OWNER, entry.id)
        results = store.semantic_search(TEST_OWNER, vec, "m")
        assert len(results) == 0

    def test_semantic_search_empty_store(self, store):
        """Search on empty store returns empty list."""
        results = store.semantic_search(TEST_OWNER, self._vec(768, 0), "m")
        assert results == []

    def test_get_entries_without_embeddings(self, store):
        """Finds entries missing embeddings for a given model."""
        now = now_utc()
        e1 = Entry(
            id=make_id(),
            type=EntryType.NOTE,
            source="test",
            tags=[],
            created=now,
            expires=None,
            data={"description": "has embedding"},
        )
        e2 = Entry(
            id=make_id(),
            type=EntryType.NOTE,
            source="test",
            tags=[],
            created=now,
            expires=None,
            data={"description": "no embedding"},
        )
        store.add(TEST_OWNER, e1)
        store.add(TEST_OWNER, e2)
        store.upsert_embedding(TEST_OWNER, e1.id, "m", 768, "h1", self._vec(768, 0))
        missing = store.get_entries_without_embeddings(TEST_OWNER, "m")
        assert len(missing) == 1
        assert missing[0].id == e2.id

    def test_get_entries_without_embeddings_skips_suppressions(self, store):
        """Suppressions are excluded from the 'needs embedding' list."""
        now = now_utc()
        sup = Entry(
            id=make_id(),
            type=EntryType.SUPPRESSION,
            source="test",
            tags=[],
            created=now,
            expires=None,
            data={"metric": "cpu", "suppress_level": "warning"},
        )
        store.add(TEST_OWNER, sup)
        missing = store.get_entries_without_embeddings(TEST_OWNER, "m")
        assert all(e.type != EntryType.SUPPRESSION for e in missing)

    def test_cascade_delete_removes_embedding(self, store):
        """Hard delete (via clear) removes embeddings too."""
        now = now_utc()
        entry = Entry(
            id=make_id(),
            type=EntryType.NOTE,
            source="test",
            tags=[],
            created=now,
            expires=None,
            data={"description": "test"},
        )
        store.add(TEST_OWNER, entry)
        store.upsert_embedding(TEST_OWNER, entry.id, "m", 768, "h1", self._vec(768, 0))
        store.clear(TEST_OWNER)
        results = store.semantic_search(TEST_OWNER, self._vec(768, 0), "m")
        assert results == []

    def test_semantic_search_limit(self, store):
        """Limit parameter caps results."""
        now = now_utc()
        vec = self._vec(768, 0)
        for i in range(5):
            entry = Entry(
                id=make_id(),
                type=EntryType.NOTE,
                source="test",
                tags=[],
                created=now,
                expires=None,
                data={"description": f"note-{i}"},
            )
            store.add(TEST_OWNER, entry)
            store.upsert_embedding(TEST_OWNER, entry.id, "m", 768, f"h{i}", vec)
        results = store.semantic_search(TEST_OWNER, vec, "m", limit=2)
        assert len(results) == 2

    def test_semantic_search_since_filter(self, store):
        """since filter narrows by updated timestamp."""
        from datetime import timedelta

        old = now_utc() - timedelta(hours=2)
        new = now_utc()
        e1 = Entry(
            id=make_id(),
            type=EntryType.NOTE,
            source="test",
            tags=[],
            created=old,
            expires=None,
            data={"description": "old"},
        )
        e2 = Entry(
            id=make_id(),
            type=EntryType.NOTE,
            source="test",
            tags=[],
            created=new,
            expires=None,
            data={"description": "new"},
        )
        store.add(TEST_OWNER, e1)
        store.add(TEST_OWNER, e2)
        vec = self._vec(768, 0)
        store.upsert_embedding(TEST_OWNER, e1.id, "m", 768, "h1", vec)
        store.upsert_embedding(TEST_OWNER, e2.id, "m", 768, "h2", vec)
        cutoff = now_utc() - timedelta(hours=1)
        results = store.semantic_search(TEST_OWNER, vec, "m", since=cutoff)
        assert len(results) == 1
        assert results[0][0].data["description"] == "new"

    def test_semantic_search_until_filter(self, store):
        """until filter narrows by updated timestamp."""
        from datetime import timedelta

        old = now_utc() - timedelta(hours=2)
        new = now_utc()
        e1 = Entry(
            id=make_id(),
            type=EntryType.NOTE,
            source="test",
            tags=[],
            created=old,
            expires=None,
            data={"description": "old"},
        )
        e2 = Entry(
            id=make_id(),
            type=EntryType.NOTE,
            source="test",
            tags=[],
            created=new,
            expires=None,
            data={"description": "new"},
        )
        store.add(TEST_OWNER, e1)
        store.add(TEST_OWNER, e2)
        vec = self._vec(768, 0)
        store.upsert_embedding(TEST_OWNER, e1.id, "m", 768, "h1", vec)
        store.upsert_embedding(TEST_OWNER, e2.id, "m", 768, "h2", vec)
        cutoff = now_utc() - timedelta(hours=1)
        results = store.semantic_search(TEST_OWNER, vec, "m", until=cutoff)
        assert len(results) == 1
        assert results[0][0].data["description"] == "old"

    def test_get_stale_embeddings(self, store):
        """Entries whose text changed after embedding are detected as stale."""
        now = now_utc()
        entry = Entry(
            id=make_id(),
            type=EntryType.NOTE,
            source="test",
            tags=[],
            created=now,
            expires=None,
            data={"description": "original text"},
        )
        store.add(TEST_OWNER, entry)
        vec = self._vec(768, 0)
        # Embed with hash of original text
        from mcp_awareness.embeddings import compose_embedding_text, text_hash

        original_hash = text_hash(compose_embedding_text(entry))
        store.upsert_embedding(TEST_OWNER, entry.id, "m", 768, original_hash, vec)
        # No stale entries yet
        assert store.get_stale_embeddings(TEST_OWNER, "m") == []
        # Update the entry text
        store.update_entry(TEST_OWNER, entry.id, {"description": "changed text"})
        # Now it should be stale
        stale = store.get_stale_embeddings(TEST_OWNER, "m")
        assert len(stale) == 1
        assert stale[0].id == entry.id

    def test_get_stale_embeddings_not_stale(self, store):
        """Entries with matching hash are not stale."""
        now = now_utc()
        entry = Entry(
            id=make_id(),
            type=EntryType.NOTE,
            source="test",
            tags=[],
            created=now,
            expires=None,
            data={"description": "stable text"},
        )
        store.add(TEST_OWNER, entry)
        from mcp_awareness.embeddings import compose_embedding_text, text_hash

        h = text_hash(compose_embedding_text(entry))
        store.upsert_embedding(TEST_OWNER, entry.id, "m", 768, h, self._vec(768, 0))
        assert store.get_stale_embeddings(TEST_OWNER, "m") == []


class TestHybridRetrieval:
    """Tests for hybrid retrieval (vector + FTS + RRF) and language support."""

    @staticmethod
    def _vec(dim: int, axis: int) -> list[float]:
        v = [0.0] * dim
        v[axis] = 1.0
        return v

    def test_entry_language_defaults_to_simple(self, store):
        """New entries default to language='simple'."""
        entry = Entry(
            id=make_id(),
            type=EntryType.NOTE,
            source="test",
            tags=["a"],
            created=now_utc(),
            data={"description": "hello world"},
        )
        store.add(TEST_OWNER, entry)
        found = store.get_entry_by_id(TEST_OWNER, entry.id)
        assert found is not None
        assert found.language == SIMPLE

    def test_entry_language_stored_and_retrieved(self, store):
        """Entries with explicit language are persisted and retrieved correctly."""
        entry = Entry(
            id=make_id(),
            type=EntryType.NOTE,
            source="test",
            tags=["lang"],
            created=now_utc(),
            data={"description": "Ein kurzer deutscher Text"},
            language="german",
        )
        store.add(TEST_OWNER, entry)
        found = store.get_entry_by_id(TEST_OWNER, entry.id)
        assert found is not None
        assert found.language == "german"

    def test_update_entry_language(self, store):
        """update_entry can change an entry's language."""
        entry = Entry(
            id=make_id(),
            type=EntryType.NOTE,
            source="test",
            tags=["a"],
            created=now_utc(),
            data={"description": "bonjour le monde"},
            language=SIMPLE,
        )
        store.add(TEST_OWNER, entry)
        result = store.update_entry(TEST_OWNER, entry.id, {"language": "french"})
        assert result is not None
        assert result.language == "french"
        found = store.get_entry_by_id(TEST_OWNER, entry.id)
        assert found is not None
        assert found.language == "french"

    def test_fts_finds_stemmed_match(self, store):
        """FTS branch matches stemmed English words via the tsv generated column."""
        entry = Entry(
            id=make_id(),
            type=EntryType.NOTE,
            source="test",
            tags=["fts"],
            created=now_utc(),
            data={"description": "The retirement planning documents are comprehensive"},
            language="english",
        )
        store.add(TEST_OWNER, entry)
        # Embed it so the hybrid CTE can find it via vector branch too
        vec = self._vec(768, 0)
        store.upsert_embedding(TEST_OWNER, entry.id, "m", 768, "h1", vec)
        # Search with a query that should match via FTS stemming
        # "retiring" stems to "retir" just like "retirement"
        results = store.semantic_search(
            TEST_OWNER,
            embedding=vec,
            model="m",
            query_text="retiring",
            query_language="english",
            limit=5,
        )
        assert len(results) >= 1
        assert results[0][0].id == entry.id

    def test_hybrid_fuses_both_branches(self, store):
        """Entries matching both vector and FTS branches rank higher than single-branch hits."""
        # Entry 1: matches both vector (axis 0) and FTS ("planning")
        e1 = Entry(
            id=make_id(),
            type=EntryType.NOTE,
            source="test",
            tags=["hybrid"],
            created=now_utc(),
            data={"description": "Financial planning for the future"},
            language="english",
        )
        store.add(TEST_OWNER, e1)
        store.upsert_embedding(TEST_OWNER, e1.id, "m", 768, "h1", self._vec(768, 0))

        # Entry 2: matches vector (axis 0) but NOT FTS ("planning")
        e2 = Entry(
            id=make_id(),
            type=EntryType.NOTE,
            source="test",
            tags=["hybrid"],
            created=now_utc(),
            data={"description": "Something completely different about cooking"},
            language="english",
        )
        store.add(TEST_OWNER, e2)
        store.upsert_embedding(TEST_OWNER, e2.id, "m", 768, "h2", self._vec(768, 0))

        results = store.semantic_search(
            TEST_OWNER,
            embedding=self._vec(768, 0),
            model="m",
            query_text="planning",
            query_language="english",
            limit=5,
        )
        assert len(results) >= 2
        ids = [e.id for e, _ in results]
        # e1 should rank first (both branches), e2 second (vector only)
        assert ids[0] == e1.id

    def test_fts_only_when_no_query_text(self, store):
        """When query_text is empty, only vector branch contributes."""
        entry = Entry(
            id=make_id(),
            type=EntryType.NOTE,
            source="test",
            tags=["vec"],
            created=now_utc(),
            data={"description": "vector only test"},
            language="english",
        )
        store.add(TEST_OWNER, entry)
        vec = self._vec(768, 3)
        store.upsert_embedding(TEST_OWNER, entry.id, "m", 768, "h1", vec)
        results = store.semantic_search(
            TEST_OWNER,
            embedding=vec,
            model="m",
            query_text="",
            query_language=SIMPLE,
            limit=5,
        )
        assert len(results) >= 1
        assert results[0][0].id == entry.id

    def test_language_in_to_dict(self):
        """Entry.to_dict includes language when not 'simple'."""
        entry = Entry(
            id="test-id",
            type=EntryType.NOTE,
            source="test",
            tags=[],
            created=now_utc(),
            data={"description": "test"},
            language="english",
        )
        d = entry.to_dict()
        assert d["language"] == "english"

    def test_language_omitted_from_to_dict_when_simple(self):
        """Entry.to_dict omits language when it's 'simple' (default)."""
        entry = Entry(
            id="test-id",
            type=EntryType.NOTE,
            source="test",
            tags=[],
            created=now_utc(),
            data={"description": "test"},
            language=SIMPLE,
        )
        d = entry.to_dict()
        assert "language" not in d

    def test_validate_regconfig_valid(self, store):
        """Valid regconfig names pass through validation."""
        assert store.validate_regconfig("english") == "english"
        assert store.validate_regconfig(SIMPLE) == SIMPLE
        assert store.validate_regconfig("french") == "french"

    def test_validate_regconfig_invalid_falls_back(self, store):
        """Invalid regconfig names fall back to 'simple'."""
        assert store.validate_regconfig("klingon") == SIMPLE
        assert store.validate_regconfig("japanese") == SIMPLE

    def test_insert_with_invalid_regconfig_falls_back(self, store):
        """Inserting an entry with an invalid regconfig silently falls back to 'simple'."""
        entry = Entry(
            id=make_id(),
            type=EntryType.NOTE,
            source="test",
            tags=["regconfig"],
            created=now_utc(),
            data={"description": "test with bad regconfig"},
            language="nonexistent_language",
        )
        store.add(TEST_OWNER, entry)
        found = store.get_entry_by_id(TEST_OWNER, entry.id)
        assert found is not None
        assert found.language == SIMPLE

    def test_regconfigs_loaded_at_init(self, store):
        """The regconfig cache is populated at store initialization."""
        assert len(store._valid_regconfigs) >= 29  # 28 snowball + simple

    def test_validate_regconfig_reload_finds_after_cache_clear(self, store):
        """After clearing cache, validate_regconfig reloads and finds valid configs."""
        store._valid_regconfigs = set()  # simulate empty cache
        assert store.validate_regconfig("english") == "english"
        assert "english" in store._valid_regconfigs

    def test_load_regconfigs_handles_error(self, store):
        """_load_regconfigs falls back to {'simple'} on error."""
        original = store._pool
        store._pool = None  # force an error
        store._load_regconfigs()
        assert store._valid_regconfigs == {SIMPLE}
        store._pool = original  # restore

    def test_update_entry_validates_regconfig(self, store):
        """update_entry validates the language regconfig before writing."""
        entry = Entry(
            id=make_id(),
            type=EntryType.NOTE,
            source="test",
            tags=["a"],
            created=now_utc(),
            data={"description": "test"},
        )
        store.add(TEST_OWNER, entry)
        result = store.update_entry(TEST_OWNER, entry.id, {"language": "klingon"})
        assert result is not None
        assert result.language == SIMPLE

    def test_upsert_by_logical_key_validates_regconfig(self, store):
        """upsert_by_logical_key validates language on the INSERT path."""
        entry = Entry(
            id=make_id(),
            type=EntryType.NOTE,
            source="test",
            tags=["a"],
            created=now_utc(),
            data={"description": "test"},
            logical_key="regconfig-test",
            language="klingon",
        )
        result, created = store.upsert_by_logical_key(TEST_OWNER, "test", "regconfig-test", entry)
        assert created
        assert result.language == SIMPLE


class TestConcurrency:
    """Tests for concurrency patterns: connection pool, cleanup threading, concurrent writes."""

    @staticmethod
    def _make_note(suffix: str) -> Entry:
        now = now_utc()
        return Entry(
            id=make_id(),
            type=EntryType.NOTE,
            source="concurrency-test",
            tags=["test"],
            created=now,
            expires=None,
            data={"description": f"concurrent note {suffix}"},
        )

    def test_concurrent_writes_no_corruption(self, store):
        """20 simultaneous add() calls must all succeed without data loss."""
        entries = [self._make_note(str(i)) for i in range(20)]
        exceptions: list[Exception] = []

        def add_entry(entry: Entry) -> None:
            try:
                store.add(TEST_OWNER, entry)
            except Exception as exc:
                exceptions.append(exc)

        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
            futures = [pool.submit(add_entry, e) for e in entries]
            concurrent.futures.wait(futures, timeout=5)

        assert exceptions == [], f"Exceptions during concurrent writes: {exceptions}"
        stored = store.get_knowledge(TEST_OWNER, tags=["test"])
        stored_ids = {e.id for e in stored}
        for entry in entries:
            assert entry.id in stored_ids, f"Entry {entry.id} missing after concurrent write"

    def test_concurrent_reads_under_writes(self, store):
        """Reads and writes in parallel must not raise exceptions."""
        # Seed some data first
        for i in range(5):
            store.add(TEST_OWNER, self._make_note(f"seed-{i}"))

        exceptions: list[Exception] = []

        def reader() -> None:
            try:
                result = store.get_knowledge(TEST_OWNER, tags=["test"])
                assert isinstance(result, list)
            except Exception as exc:
                exceptions.append(exc)

        def writer(idx: int) -> None:
            try:
                store.add(TEST_OWNER, self._make_note(f"parallel-{idx}"))
            except Exception as exc:
                exceptions.append(exc)

        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
            futures = []
            for i in range(10):
                futures.append(pool.submit(reader))
                futures.append(pool.submit(writer, i))
            concurrent.futures.wait(futures, timeout=5)

        assert exceptions == [], f"Exceptions during concurrent read/write: {exceptions}"
        # All seed + parallel entries should exist
        stored = store.get_knowledge(TEST_OWNER, tags=["test"])
        assert len(stored) >= 15  # 5 seed + 10 parallel

    def test_cleanup_thread_debouncing(self, store):
        """Rapid _cleanup_expired() calls must be debounced — at most 1-2 threads spawned."""
        original_interval = store._cleanup_interval
        store._cleanup_interval = 0  # disable debounce delay for test
        store._last_cleanup = 0.0  # reset so first call fires
        spawned_threads: list[threading.Thread] = []
        original_do_cleanup = store._do_cleanup

        def tracking_cleanup() -> None:
            spawned_threads.append(threading.current_thread())
            original_do_cleanup()

        try:
            store._do_cleanup = tracking_cleanup  # type: ignore[assignment]
            for _ in range(10):
                store._cleanup_expired()
                # The guard checks is_alive(), so thread must finish before next can spawn.
                # With interval=0, debounce passes but alive-guard still limits.
            # Wait for any spawned thread to finish
            for t in spawned_threads:
                t.join(timeout=2)

            assert store._cleanup_thread is not None
            # With debounce=0, each call can spawn IF the previous finished,
            # but the test runs fast enough that we expect a small number.
            assert len(spawned_threads) >= 1
        finally:
            store._cleanup_interval = original_interval
            store._do_cleanup = original_do_cleanup  # type: ignore[assignment]

    def test_cleanup_guard_prevents_accumulation(self, store):
        """While a cleanup thread is running, new calls must not spawn another."""
        original_interval = store._cleanup_interval
        store._cleanup_interval = 0
        store._last_cleanup = 0.0

        barrier = threading.Event()
        spawned_count = 0
        lock = threading.Lock()

        def slow_cleanup() -> None:
            nonlocal spawned_count
            with lock:
                spawned_count += 1
            barrier.wait(timeout=3)  # block until released

        try:
            store._do_cleanup = slow_cleanup  # type: ignore[assignment]

            # First call should spawn a thread
            store._cleanup_expired()
            time.sleep(0.05)  # let thread start

            # Subsequent calls while slow_cleanup is blocking should be no-ops
            for _ in range(5):
                store._last_cleanup = 0.0  # reset debounce so guard is the only blocker
                store._cleanup_expired()

            assert spawned_count == 1, f"Expected 1 cleanup thread but {spawned_count} were spawned"
        finally:
            barrier.set()  # unblock the slow cleanup
            store._cleanup_interval = original_interval
            if store._cleanup_thread is not None:
                store._cleanup_thread.join(timeout=2)

    def test_upsert_by_logical_key_creates_then_updates(self, store):
        """First upsert creates the entry; second upsert updates it in place."""
        source = "upsert-single-conn"
        logical_key = "lk-create-update"
        now = now_utc()

        # First call — should create
        entry1 = Entry(
            id=make_id(),
            type=EntryType.NOTE,
            source=source,
            tags=["v1"],
            created=now,
            expires=None,
            data={"description": "original description"},
            logical_key=logical_key,
        )
        result1, created1 = store.upsert_by_logical_key(TEST_OWNER, source, logical_key, entry1)
        assert created1 is True
        assert result1.id == entry1.id
        assert result1.data["description"] == "original description"

        # Second call — same logical_key, different tags and description
        entry2 = Entry(
            id=make_id(),
            type=EntryType.NOTE,
            source=source,
            tags=["v2"],
            created=now,
            expires=None,
            data={"description": "updated description"},
            logical_key=logical_key,
        )
        result2, created2 = store.upsert_by_logical_key(TEST_OWNER, source, logical_key, entry2)
        assert created2 is False
        # Should keep the original entry's id
        assert result2.id == entry1.id
        # Tags and description should reflect the update
        assert result2.tags == ["v2"]
        assert result2.data["description"] == "updated description"
        # Changelog should record the change
        assert "changelog" in result2.data
        assert len(result2.data["changelog"]) >= 1

        # Verify only one entry exists for this logical_key
        results = store.get_knowledge(TEST_OWNER, source=source)
        matching = [e for e in results if e.logical_key == logical_key]
        assert len(matching) == 1

    def test_concurrent_upsert_by_logical_key(self, store):
        """Concurrent upserts with same source + logical_key must not create duplicates."""
        source = "upsert-race"
        logical_key = "singleton"
        exceptions: list[Exception] = []

        def do_upsert(idx: int) -> None:
            try:
                now = now_utc()
                entry = Entry(
                    id=make_id(),
                    type=EntryType.NOTE,
                    source=source,
                    tags=["upsert-test"],
                    created=now,
                    expires=None,
                    data={"description": f"upsert attempt {idx}"},
                    logical_key=logical_key,
                )
                store.upsert_by_logical_key(TEST_OWNER, source, logical_key, entry)
            except Exception as exc:
                exceptions.append(exc)

        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
            futures = [pool.submit(do_upsert, i) for i in range(10)]
            concurrent.futures.wait(futures, timeout=5)

        assert exceptions == [], f"Exceptions during concurrent upsert: {exceptions}"
        # Only one entry should exist for this source + logical_key
        results = store.get_knowledge(TEST_OWNER, source=source)
        matching = [e for e in results if e.logical_key == logical_key]
        assert len(matching) == 1, (
            f"Expected 1 entry for logical_key={logical_key} but found {len(matching)}"
        )


class TestEmbeddingRoundTrip:
    """End-to-end tests for the embedding pipeline.

    Verifies the full flow: compose_embedding_text → text_hash →
    upsert_embedding → semantic_search, plus stale detection and
    re-embedding after content changes.  Uses hand-crafted unit vectors
    so no real embedding model (Ollama) is needed.
    """

    @staticmethod
    def _vec(dim: int, axis: int) -> list[float]:
        """Unit vector along a specific axis in `dim`-dimensional space."""
        v = [0.0] * dim
        v[axis] = 1.0
        return v

    # -- 1. Full round-trip: compose → store → search -----------------------

    def test_compose_store_search_round_trip(self, store):
        """Create entries, compose text, hash, store embeddings, then search."""
        from mcp_awareness.embeddings import compose_embedding_text, text_hash

        now = now_utc()
        entries = [
            Entry(
                id=make_id(),
                type=EntryType.NOTE,
                source="nas",
                tags=["infra"],
                created=now,
                expires=None,
                data={"description": "NAS storage pool is 85% full"},
            ),
            Entry(
                id=make_id(),
                type=EntryType.NOTE,
                source="personal",
                tags=["health"],
                created=now,
                expires=None,
                data={"description": "Morning run completed 5km in 28 minutes"},
            ),
            Entry(
                id=make_id(),
                type=EntryType.NOTE,
                source="calendar",
                tags=["work"],
                created=now,
                expires=None,
                data={"description": "Team standup moved to 10am starting Monday"},
            ),
        ]

        # Compose text — each should be distinct
        texts = [compose_embedding_text(e) for e in entries]
        assert len(set(texts)) == 3, "composed texts should be distinct"
        for t in texts:
            assert len(t) > 0, "composed text should be non-empty"

        # Hash — each should be distinct (distinct inputs → distinct hashes)
        hashes = [text_hash(t) for t in texts]
        assert len(set(hashes)) == 3, "hashes should be distinct"

        # Store entries and embeddings on orthogonal axes
        for i, entry in enumerate(entries):
            store.add(TEST_OWNER, entry)
            store.upsert_embedding(TEST_OWNER, entry.id, "m", 768, hashes[i], self._vec(768, i))

        # Search with each vector — should find its own entry first
        for i, entry in enumerate(entries):
            results = store.semantic_search(TEST_OWNER, self._vec(768, i), "m", limit=3)
            assert len(results) == 3
            assert results[0][0].id == entry.id
            assert results[0][1] > 0  # RRF score

    # -- 2. Stale embedding detection ----------------------------------------

    def test_stale_embedding_detection(self, store):
        """Updating an entry's description makes its embedding stale."""
        from mcp_awareness.embeddings import compose_embedding_text, text_hash

        now = now_utc()
        entry = Entry(
            id=make_id(),
            type=EntryType.NOTE,
            source="test",
            tags=["project"],
            created=now,
            expires=None,
            data={"description": "original description"},
        )
        store.add(TEST_OWNER, entry)

        original_hash = text_hash(compose_embedding_text(entry))
        store.upsert_embedding(TEST_OWNER, entry.id, "m", 768, original_hash, self._vec(768, 0))

        # Not stale yet
        assert store.get_stale_embeddings(TEST_OWNER, "m") == []

        # Update the entry — changes the composed text
        store.update_entry(
            TEST_OWNER, entry.id, {"description": "completely different description"}
        )

        # Now it should be stale
        stale = store.get_stale_embeddings(TEST_OWNER, "m")
        assert len(stale) == 1
        assert stale[0].id == entry.id

    # -- 3. Embedding update (re-embed after content change) -----------------

    def test_reembed_after_content_change(self, store):
        """After re-computing and storing a new embedding, entry is no longer stale."""
        from mcp_awareness.embeddings import compose_embedding_text, text_hash

        now = now_utc()
        entry = Entry(
            id=make_id(),
            type=EntryType.NOTE,
            source="test",
            tags=[],
            created=now,
            expires=None,
            data={"description": "version one"},
        )
        store.add(TEST_OWNER, entry)

        h1 = text_hash(compose_embedding_text(entry))
        store.upsert_embedding(TEST_OWNER, entry.id, "m", 768, h1, self._vec(768, 0))
        assert store.get_stale_embeddings(TEST_OWNER, "m") == []

        # Mutate entry
        store.update_entry(TEST_OWNER, entry.id, {"description": "version two"})
        assert len(store.get_stale_embeddings(TEST_OWNER, "m")) == 1

        # Re-embed: fetch updated entry, recompose, re-hash, re-store
        updated = store.get_entry_by_id(TEST_OWNER, entry.id)
        assert updated is not None
        new_text = compose_embedding_text(updated)
        h2 = text_hash(new_text)
        assert h2 != h1
        store.upsert_embedding(TEST_OWNER, entry.id, "m", 768, h2, self._vec(768, 1))

        # No longer stale
        assert store.get_stale_embeddings(TEST_OWNER, "m") == []

        # Still searchable
        results = store.semantic_search(TEST_OWNER, self._vec(768, 1), "m", limit=1)
        assert len(results) == 1
        assert results[0][0].id == entry.id

    # -- 4. Search with filters ----------------------------------------------

    def test_search_filters_combined(self, store):
        """Source, tag, and entry_type filters narrow semantic search results."""
        now = now_utc()
        note_infra = Entry(
            id=make_id(),
            type=EntryType.NOTE,
            source="nas",
            tags=["infra"],
            created=now,
            expires=None,
            data={"description": "disk usage high"},
        )
        note_personal = Entry(
            id=make_id(),
            type=EntryType.NOTE,
            source="personal",
            tags=["health"],
            created=now,
            expires=None,
            data={"description": "went for a run"},
        )
        alert_infra = Entry(
            id=make_id(),
            type=EntryType.ALERT,
            source="nas",
            tags=["infra"],
            created=now,
            expires=None,
            data={
                "alert_id": "disk1",
                "message": "disk failing",
                "level": "critical",
                "resolved": False,
            },
        )

        # Same vector for all — filters are what differentiate results
        vec = self._vec(768, 0)
        for i, e in enumerate([note_infra, note_personal, alert_infra]):
            store.add(TEST_OWNER, e)
            store.upsert_embedding(TEST_OWNER, e.id, "m", 768, f"h{i}", vec)

        # Source filter
        results = store.semantic_search(TEST_OWNER, vec, "m", source="nas")
        assert len(results) == 2
        assert all(r[0].source == "nas" for r in results)

        # Tag filter
        results = store.semantic_search(TEST_OWNER, vec, "m", tags=["health"])
        assert len(results) == 1
        assert results[0][0].id == note_personal.id

        # Entry type filter
        results = store.semantic_search(TEST_OWNER, vec, "m", entry_type=EntryType.ALERT)
        assert len(results) == 1
        assert results[0][0].id == alert_infra.id

        # Combined: source + type
        results = store.semantic_search(
            TEST_OWNER, vec, "m", source="nas", entry_type=EntryType.NOTE
        )
        assert len(results) == 1
        assert results[0][0].id == note_infra.id

    # -- 5. compose_embedding_text covers all entry types --------------------

    def test_compose_text_covers_entry_types(self, store):
        """compose_embedding_text produces non-empty, distinct text for each type."""
        from mcp_awareness.embeddings import compose_embedding_text

        now = now_utc()
        entries = [
            Entry(
                id=make_id(),
                type=EntryType.NOTE,
                source="test-note",
                tags=["tag-a"],
                created=now,
                expires=None,
                data={"description": "a note about something"},
            ),
            Entry(
                id=make_id(),
                type=EntryType.PATTERN,
                source="test-pattern",
                tags=["tag-b"],
                created=now,
                expires=None,
                data={"description": "when CPU spikes", "effect": "fans run loud"},
            ),
            Entry(
                id=make_id(),
                type=EntryType.CONTEXT,
                source="test-context",
                tags=["tag-c"],
                created=now,
                expires=None,
                data={"description": "working on embedding tests"},
            ),
            Entry(
                id=make_id(),
                type=EntryType.INTENTION,
                source="test-intention",
                tags=["tag-d"],
                created=now,
                expires=None,
                data={"goal": "finish the PR by end of day", "description": "embedding round-trip"},
            ),
        ]

        texts = [compose_embedding_text(e) for e in entries]

        # All non-empty
        for t in texts:
            assert len(t) > 0

        # All distinct
        assert len(set(texts)) == len(entries)

        # Each includes type, source, and tags
        for entry, text in zip(entries, texts, strict=True):
            assert entry.type.value in text
            assert entry.source in text
            for tag in entry.tags:
                assert tag in text


# ------------------------------------------------------------------
# Owner isolation tests
# ------------------------------------------------------------------


class TestOwnerIsolation:
    @pytest.fixture(autouse=True)
    def _cleanup_owners(self, store):
        yield
        for owner in ("alice", "bob"):
            store.clear(owner)

    def test_entries_isolated_by_owner(self, store):
        """Entries from one owner are not visible to another."""
        entry = Entry(
            id=make_id(),
            type=EntryType.NOTE,
            source="test",
            tags=["t"],
            created=now_utc(),
            expires=None,
            data={"description": "alice's"},
        )
        store.add("alice", entry)
        assert len(store.get_entries("alice")) == 1
        assert len(store.get_entries("bob")) == 0

    def test_stats_scoped_to_owner(self, store):
        entry = Entry(
            id=make_id(),
            type=EntryType.NOTE,
            source="test",
            tags=["t"],
            created=now_utc(),
            expires=None,
            data={"description": "x"},
        )
        store.add("alice", entry)
        assert store.get_stats("alice")["total"] == 1
        assert store.get_stats("bob")["total"] == 0

    def test_soft_delete_isolated(self, store):
        entry = Entry(
            id=make_id(),
            type=EntryType.NOTE,
            source="test",
            tags=["t"],
            created=now_utc(),
            expires=None,
            data={"description": "x"},
        )
        store.add("alice", entry)
        assert store.soft_delete_by_id("bob", entry.id) is False
        assert store.soft_delete_by_id("alice", entry.id) is True


# ---------------------------------------------------------------------------
# Batch query methods
# ---------------------------------------------------------------------------


def test_get_all_statuses(store):
    """get_all_statuses returns {source: Entry} for all sources."""
    store.upsert_status(TEST_OWNER, "nas", ["infra"], {"metrics": {"cpu": 50}, "ttl_sec": 3600})
    store.upsert_status(TEST_OWNER, "ci", ["cicd"], {"metrics": {}, "ttl_sec": 3600})
    result = store.get_all_statuses(TEST_OWNER)
    assert set(result.keys()) == {"nas", "ci"}
    assert result["nas"].source == "nas"
    assert result["ci"].source == "ci"


def test_get_all_statuses_empty(store):
    """get_all_statuses returns empty dict when no statuses exist."""
    assert store.get_all_statuses(TEST_OWNER) == {}


def test_get_all_active_alerts(store):
    """get_all_active_alerts returns {source: [Entry]} for all sources."""
    store.upsert_alert(
        TEST_OWNER,
        "nas",
        ["infra"],
        "a1",
        {"alert_id": "a1", "level": "warning", "message": "NAS issue", "resolved": False},
    )
    store.upsert_alert(
        TEST_OWNER,
        "ci",
        ["cicd"],
        "a2",
        {"alert_id": "a2", "level": "warning", "message": "CI issue", "resolved": False},
    )
    store.upsert_alert(
        TEST_OWNER,
        "nas",
        ["infra"],
        "a3",
        {"alert_id": "a3", "level": "critical", "message": "NAS critical", "resolved": False},
    )
    result = store.get_all_active_alerts(TEST_OWNER)
    assert set(result.keys()) == {"nas", "ci"}
    assert len(result["nas"]) == 2
    assert len(result["ci"]) == 1


def test_get_all_active_alerts_excludes_resolved(store):
    """get_all_active_alerts excludes resolved alerts."""
    store.upsert_alert(
        TEST_OWNER,
        "nas",
        ["infra"],
        "a1",
        {"alert_id": "a1", "level": "warning", "message": "resolved", "resolved": True},
    )
    result = store.get_all_active_alerts(TEST_OWNER)
    assert result == {}


def test_get_all_active_suppressions(store):
    """get_all_active_suppressions groups by source, includes global."""
    store.add(
        TEST_OWNER,
        Entry(
            id=make_id(),
            type=EntryType.SUPPRESSION,
            source="nas",
            tags=["infra"],
            created=now_utc(),
            expires=None,
            data={"metric": "cpu", "reason": "maintenance"},
        ),
    )
    store.add(
        TEST_OWNER,
        Entry(
            id=make_id(),
            type=EntryType.SUPPRESSION,
            source="",
            tags=[],
            created=now_utc(),
            expires=None,
            data={"metric": "all", "reason": "global"},
        ),
    )
    result = store.get_all_active_suppressions(TEST_OWNER)
    assert "nas" in result
    assert "" in result
    assert len(result["nas"]) == 1
    assert len(result[""]) == 1


def test_get_all_patterns(store):
    """get_all_patterns groups by source, includes global."""
    store.add(
        TEST_OWNER,
        Entry(
            id=make_id(),
            type=EntryType.PATTERN,
            source="nas",
            tags=["infra"],
            created=now_utc(),
            expires=None,
            data={"effect": "CPU spike during backup", "conditions": {"hour_range": "02:00-04:00"}},
        ),
    )
    store.add(
        TEST_OWNER,
        Entry(
            id=make_id(),
            type=EntryType.PATTERN,
            source="",
            tags=[],
            created=now_utc(),
            expires=None,
            data={"effect": "Global pattern", "conditions": {}},
        ),
    )
    result = store.get_all_patterns(TEST_OWNER)
    assert "nas" in result
    assert "" in result


# ------------------------------------------------------------------
# find_schema tests
# ------------------------------------------------------------------

SYSTEM_OWNER = "_system"


def _make_schema_entry(logical_key: str, schema_body: dict) -> Entry:
    return Entry(
        id=make_id(),
        type=EntryType.SCHEMA,
        source="test",
        tags=[],
        created=now_utc(),
        data={
            "family": logical_key.rsplit(":", 1)[0] if ":" in logical_key else logical_key,
            "version": logical_key.rsplit(":", 1)[1] if ":" in logical_key else "1.0.0",
            "schema": schema_body,
            "description": "test schema",
            "learned_from": "test",
        },
        logical_key=logical_key,
    )


def test_find_schema_returns_caller_owned(store):
    """find_schema returns an entry when caller owns it."""
    entry = _make_schema_entry("s:test:1.0.0", {"type": "object"})
    store.add(TEST_OWNER, entry)
    found = store.find_schema(TEST_OWNER, "s:test:1.0.0")
    assert found is not None
    assert found.data["family"] == "s:test"
    assert found.data["schema"] == {"type": "object"}


def test_find_schema_system_fallback(store):
    """find_schema falls back to _system-owned schema when caller has none."""
    entry = _make_schema_entry("s:test:1.0.0", {"type": "object"})
    store.add(SYSTEM_OWNER, entry)
    found = store.find_schema(TEST_OWNER, "s:test:1.0.0")
    assert found is not None
    assert found.data["schema"] == {"type": "object"}


def test_find_schema_caller_wins_over_system(store):
    """find_schema prefers caller's schema over _system's when both exist."""
    system_entry = _make_schema_entry("s:test:1.0.0", {"type": "object"})
    caller_entry = _make_schema_entry("s:test:1.0.0", {"type": "string"})
    store.add(SYSTEM_OWNER, system_entry)
    store.add(TEST_OWNER, caller_entry)
    found = store.find_schema(TEST_OWNER, "s:test:1.0.0")
    assert found is not None
    assert found.data["schema"] == {"type": "string"}


def test_find_schema_returns_none_when_missing(store):
    """find_schema returns None when no matching schema exists for caller or _system."""
    assert store.find_schema(TEST_OWNER, "s:nonexistent:1.0.0") is None


def test_find_schema_excludes_soft_deleted(store):
    """find_schema does not return soft-deleted entries."""
    entry = _make_schema_entry("s:test:1.0.0", {"type": "object"})
    stored = store.add(TEST_OWNER, entry)
    store.soft_delete_by_id(TEST_OWNER, stored.id)
    assert store.find_schema(TEST_OWNER, "s:test:1.0.0") is None


# ------------------------------------------------------------------
# count_records_referencing tests
# ------------------------------------------------------------------


def _make_record_entry(logical_key: str, schema_ref: str, schema_version: str, content) -> Entry:
    return Entry(
        id=make_id(),
        type=EntryType.RECORD,
        source="test",
        tags=[],
        created=now_utc(),
        data={
            "schema_ref": schema_ref,
            "schema_version": schema_version,
            "content": content,
            "description": "test record",
            "learned_from": "test",
        },
        logical_key=logical_key,
    )


def test_count_records_referencing_returns_zero_when_none(store):
    count, ids = store.count_records_referencing(TEST_OWNER, "s:test:1.0.0")
    assert count == 0
    assert ids == []


def test_count_records_referencing_counts_matching_records(store):
    for i in range(3):
        store.add(TEST_OWNER, _make_record_entry(f"rec-{i}", "s:test", "1.0.0", {"i": i}))
    count, ids = store.count_records_referencing(TEST_OWNER, "s:test:1.0.0")
    assert count == 3
    assert len(ids) == 3


def test_count_records_referencing_excludes_soft_deleted(store):
    entry = _make_record_entry("rec-1", "s:test", "1.0.0", {})
    store.add(TEST_OWNER, entry)
    store.soft_delete_by_id(TEST_OWNER, entry.id)
    count, ids = store.count_records_referencing(TEST_OWNER, "s:test:1.0.0")
    assert count == 0
    assert ids == []


def test_count_records_referencing_ignores_other_versions(store):
    store.add(TEST_OWNER, _make_record_entry("rec-1", "s:test", "1.0.0", {}))
    store.add(TEST_OWNER, _make_record_entry("rec-2", "s:test", "2.0.0", {}))
    count, _ = store.count_records_referencing(TEST_OWNER, "s:test:1.0.0")
    assert count == 1


def test_count_records_referencing_caps_id_list_at_ten(store):
    for i in range(15):
        store.add(TEST_OWNER, _make_record_entry(f"rec-{i}", "s:test", "1.0.0", {"i": i}))
    count, ids = store.count_records_referencing(TEST_OWNER, "s:test:1.0.0")
    assert count == 15
    assert len(ids) == 10
