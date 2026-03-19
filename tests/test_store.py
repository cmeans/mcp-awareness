"""Tests for the SQLite storage backend."""

import sqlite3

import pytest

from mcp_awareness.schema import Entry, EntryType, make_id, now_iso
from mcp_awareness.store import AwarenessStore


@pytest.fixture
def store(tmp_path):
    return AwarenessStore(tmp_path / "test-store.db")


def test_empty_store(store):
    assert store.get_sources() == []
    assert store.get_active_alerts() == []
    assert store.get_knowledge() == []


def test_upsert_status(store):
    entry = store.upsert_status("nas", ["infra"], {"metrics": {"cpu": 50}, "ttl_sec": 120})
    assert entry.type == EntryType.STATUS
    assert entry.source == "nas"
    assert store.get_sources() == ["nas"]


def test_upsert_status_replaces(store):
    store.upsert_status("nas", ["infra"], {"metrics": {"cpu": 50}, "ttl_sec": 120})
    store.upsert_status("nas", ["infra"], {"metrics": {"cpu": 70}, "ttl_sec": 120})
    statuses = store.get_entries(entry_type=EntryType.STATUS, source="nas")
    assert len(statuses) == 1
    assert statuses[0].data["metrics"]["cpu"] == 70


def test_upsert_alert_new(store):
    entry = store.upsert_alert(
        "nas",
        ["infra"],
        "cpu-warn-1",
        {"alert_id": "cpu-warn-1", "level": "warning", "message": "CPU high", "resolved": False},
    )
    assert entry.type == EntryType.ALERT
    alerts = store.get_active_alerts()
    assert len(alerts) == 1


def test_upsert_alert_updates(store):
    store.upsert_alert(
        "nas",
        ["infra"],
        "cpu-warn-1",
        {"alert_id": "cpu-warn-1", "level": "warning", "message": "CPU high", "resolved": False},
    )
    store.upsert_alert(
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
    alerts = store.get_active_alerts()
    assert len(alerts) == 1
    assert alerts[0].data["level"] == "critical"
    assert alerts[0].data["message"] == "CPU very high"


def test_resolved_alert_not_active(store):
    store.upsert_alert(
        "nas",
        ["infra"],
        "cpu-warn-1",
        {"alert_id": "cpu-warn-1", "level": "warning", "message": "CPU high", "resolved": True},
    )
    assert store.get_active_alerts() == []


def test_get_active_alerts_by_source(store):
    store.upsert_alert(
        "nas",
        ["infra"],
        "a1",
        {"alert_id": "a1", "level": "warning", "message": "NAS issue", "resolved": False},
    )
    store.upsert_alert(
        "ci",
        ["cicd"],
        "a2",
        {"alert_id": "a2", "level": "warning", "message": "CI issue", "resolved": False},
    )
    assert len(store.get_active_alerts("nas")) == 1
    assert len(store.get_active_alerts("ci")) == 1
    assert len(store.get_active_alerts("other")) == 0


def test_add_and_get_patterns(store):
    now = now_iso()
    entry = Entry(
        id=make_id(),
        type=EntryType.PATTERN,
        source="nas",
        tags=["infra"],
        created=now,
        updated=now,
        expires=None,
        data={
            "description": "test",
            "conditions": {},
            "effect": "suppress test",
            "learned_from": "test",
        },
    )
    store.add(entry)
    patterns = store.get_patterns("nas")
    assert len(patterns) == 1
    assert patterns[0].data["description"] == "test"


def test_get_patterns_filtered_by_source(store):
    now = now_iso()
    for src in ("nas", "ci"):
        store.add(
            Entry(
                id=make_id(),
                type=EntryType.PATTERN,
                source=src,
                tags=[],
                created=now,
                updated=now,
                expires=None,
                data={
                    "description": f"{src} pattern",
                    "conditions": {},
                    "effect": "",
                    "learned_from": "test",
                },
            )
        )
    assert len(store.get_patterns("nas")) == 1
    assert len(store.get_patterns("ci")) == 1
    assert len(store.get_patterns()) == 2


def test_suppressions(store):
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    expires = (now + timedelta(hours=1)).isoformat()
    entry = Entry(
        id=make_id(),
        type=EntryType.SUPPRESSION,
        source="nas",
        tags=["infra"],
        created=now.isoformat(),
        updated=now.isoformat(),
        expires=expires,
        data={
            "metric": "cpu_pct",
            "suppress_level": "warning",
            "escalation_override": True,
            "reason": "test",
        },
    )
    store.add(entry)
    assert store.count_active_suppressions() == 1
    assert len(store.get_active_suppressions("nas")) == 1


def test_global_suppression_matches_any_source(store):
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    expires = (now + timedelta(hours=1)).isoformat()
    entry = Entry(
        id=make_id(),
        type=EntryType.SUPPRESSION,
        source="",
        tags=[],
        created=now.isoformat(),
        updated=now.isoformat(),
        expires=expires,
        data={
            "metric": None,
            "suppress_level": "warning",
            "escalation_override": True,
            "reason": "global",
        },
    )
    store.add(entry)
    assert len(store.get_active_suppressions("nas")) == 1
    assert len(store.get_active_suppressions("ci")) == 1


def test_expired_entries_cleaned(store):
    from datetime import datetime, timedelta, timezone

    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    entry = Entry(
        id=make_id(),
        type=EntryType.SUPPRESSION,
        source="nas",
        tags=[],
        created=past,
        updated=past,
        expires=past,
        data={"metric": "cpu_pct", "suppress_level": "warning"},
    )
    store.add(entry)  # add doesn't check expiry of the entry being added right away
    # Force cleanup to run despite debounce (add() just ran cleanup moments ago)
    store._last_cleanup = 0.0
    # count_active_suppressions calls _cleanup_expired
    assert store.count_active_suppressions() == 0


def test_knowledge_includes_patterns_context_preferences(store):
    now = now_iso()
    for t in (EntryType.PATTERN, EntryType.CONTEXT, EntryType.PREFERENCE):
        store.add(
            Entry(
                id=make_id(),
                type=t,
                source="s",
                tags=["t"],
                created=now,
                updated=now,
                expires=None,
                data={},
            )
        )
    assert len(store.get_knowledge()) == 3


def test_knowledge_filtered_by_tags(store):
    now = now_iso()
    store.add(
        Entry(
            id=make_id(),
            type=EntryType.PATTERN,
            source="s",
            tags=["infra"],
            created=now,
            updated=now,
            expires=None,
            data={},
        )
    )
    store.add(
        Entry(
            id=make_id(),
            type=EntryType.CONTEXT,
            source="s",
            tags=["calendar"],
            created=now,
            updated=now,
            expires=None,
            data={},
        )
    )
    assert len(store.get_knowledge(tags=["infra"])) == 1
    assert len(store.get_knowledge(tags=["calendar"])) == 1


def test_persistence(tmp_path):
    path = tmp_path / "persist.db"
    store1 = AwarenessStore(path)
    store1.upsert_status("nas", ["infra"], {"metrics": {"cpu": 42}, "ttl_sec": 120})

    store2 = AwarenessStore(path)
    assert store2.get_sources() == ["nas"]
    status = store2.get_latest_status("nas")
    assert status.data["metrics"]["cpu"] == 42


def test_wal_mode(tmp_path):
    path = tmp_path / "wal.db"
    AwarenessStore(path)
    conn = sqlite3.connect(str(path))
    result = conn.execute("PRAGMA journal_mode").fetchone()[0]
    conn.close()
    assert result == "wal"


def test_upsert_preference_deduplicates(store):
    entry1 = store.upsert_preference(
        key="alert_verbosity",
        scope="global",
        tags=["prefs"],
        data={"key": "alert_verbosity", "value": "verbose", "scope": "global"},
    )
    entry2 = store.upsert_preference(
        key="alert_verbosity",
        scope="global",
        tags=["prefs"],
        data={"key": "alert_verbosity", "value": "one_sentence", "scope": "global"},
    )
    prefs = store.get_entries(entry_type=EntryType.PREFERENCE)
    assert len(prefs) == 1
    assert prefs[0].data["value"] == "one_sentence"
    assert entry1.id == entry2.id


def test_clear(store):
    store.upsert_status("nas", [], {"metrics": {}, "ttl_sec": 60})
    store.clear()
    assert store.get_sources() == []
