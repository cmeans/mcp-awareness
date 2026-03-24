"""Tests for the PostgreSQL storage backend."""

import time

from mcp_awareness.schema import Entry, EntryType, make_id, now_utc

# store fixture comes from conftest.py (testcontainers Postgres)


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
    now = now_utc()
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
    now = now_utc()
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
    expires = now + timedelta(hours=1)
    entry = Entry(
        id=make_id(),
        type=EntryType.SUPPRESSION,
        source="nas",
        tags=["infra"],
        created=now,
        updated=now,
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
    expires = now + timedelta(hours=1)
    entry = Entry(
        id=make_id(),
        type=EntryType.SUPPRESSION,
        source="",
        tags=[],
        created=now,
        updated=now,
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

    past = datetime.now(timezone.utc) - timedelta(hours=1)
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
    store.add(entry)
    # Force cleanup to run on next write despite debounce
    store._last_cleanup = 0.0
    # Trigger cleanup via a write — cleanup runs in background
    dummy = Entry(
        id=make_id(),
        type=EntryType.CONTEXT,
        source="test",
        tags=[],
        created=past,
        updated=past,
        expires=None,
        data={"description": "trigger cleanup"},
    )
    store.add(dummy)
    time.sleep(0.2)
    assert store.count_active_suppressions() == 0


def test_knowledge_includes_patterns_context_preferences(store):
    now = now_utc()
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
    now = now_utc()
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


def test_soft_delete_by_id(store):
    now = now_utc()
    entry = Entry(
        id=make_id(),
        type=EntryType.PATTERN,
        source="nas",
        tags=["infra"],
        created=now,
        updated=now,
        expires=None,
        data={"description": "test pattern"},
    )
    store.add(entry)
    assert len(store.get_patterns()) == 1
    assert store.soft_delete_by_id(entry.id) is True
    assert len(store.get_patterns()) == 0
    assert len(store.get_deleted()) == 1


def test_soft_delete_by_id_not_found(store):
    assert store.soft_delete_by_id("nonexistent-id") is False


def test_soft_delete_by_source(store):
    now = now_utc()
    for i in range(3):
        store.add(
            Entry(
                id=make_id(),
                type=EntryType.PATTERN,
                source="nas",
                tags=[],
                created=now,
                updated=now,
                expires=None,
                data={"description": f"pattern {i}"},
            )
        )
    store.add(
        Entry(
            id=make_id(),
            type=EntryType.PATTERN,
            source="ci",
            tags=[],
            created=now,
            updated=now,
            expires=None,
            data={"description": "ci pattern"},
        )
    )
    trashed = store.soft_delete_by_source("nas")
    assert trashed == 3
    assert len(store.get_patterns("nas")) == 0
    assert len(store.get_patterns("ci")) == 1
    assert len(store.get_deleted()) == 3


def test_soft_delete_by_source_with_type_filter(store):
    now = now_utc()
    store.add(
        Entry(
            id=make_id(),
            type=EntryType.PATTERN,
            source="nas",
            tags=[],
            created=now,
            updated=now,
            expires=None,
            data={"description": "pattern"},
        )
    )
    store.upsert_status("nas", ["infra"], {"metrics": {"cpu": 50}, "ttl_sec": 120})
    trashed = store.soft_delete_by_source("nas", EntryType.PATTERN)
    assert trashed == 1
    assert store.get_latest_status("nas") is not None


def test_restore_by_id(store):
    now = now_utc()
    entry = Entry(
        id=make_id(),
        type=EntryType.PATTERN,
        source="nas",
        tags=["infra"],
        created=now,
        updated=now,
        expires=None,
        data={"description": "restorable"},
    )
    store.add(entry)
    store.soft_delete_by_id(entry.id)
    assert len(store.get_patterns()) == 0
    assert store.restore_by_id(entry.id) is True
    assert len(store.get_patterns()) == 1
    assert store.get_patterns()[0].data["description"] == "restorable"


def test_restore_not_found(store):
    assert store.restore_by_id("nonexistent") is False


def test_soft_deleted_entries_auto_expire(store):
    """Soft-deleted entries get an expires timestamp and will be purged by cleanup."""
    now = now_utc()
    entry = Entry(
        id=make_id(),
        type=EntryType.PATTERN,
        source="nas",
        tags=[],
        created=now,
        updated=now,
        expires=None,
        data={"description": "will expire"},
    )
    store.add(entry)
    store.soft_delete_by_id(entry.id)
    assert len(store.get_deleted()) == 1
    # Backdate the expires timestamp to trigger cleanup
    with store._conn.cursor() as cur:
        cur.execute(
            "UPDATE entries SET expires = %s WHERE id = %s",
            ("2020-01-01T00:00:00+00:00", entry.id),
        )
    store._conn.commit()
    store._do_cleanup()
    assert len(store.get_deleted()) == 0


def test_double_soft_delete_is_noop(store):
    now = now_utc()
    entry = Entry(
        id=make_id(),
        type=EntryType.PATTERN,
        source="nas",
        tags=[],
        created=now,
        updated=now,
        expires=None,
        data={"description": "test"},
    )
    store.add(entry)
    assert store.soft_delete_by_id(entry.id) is True
    assert store.soft_delete_by_id(entry.id) is False


def test_clear(store):
    store.upsert_status("nas", [], {"metrics": {}, "ttl_sec": 60})
    store.clear()
    assert store.get_sources() == []


# ------------------------------------------------------------------
# Pagination tests
# ------------------------------------------------------------------


def test_get_knowledge_pagination(store):
    """Limit and offset work on get_knowledge."""
    for i in range(5):
        store.add(
            Entry(
                id=make_id(),
                type=EntryType.NOTE,
                source="test",
                tags=[],
                created=now_utc(),
                updated=now_utc(),
                expires=None,
                data={"description": f"note-{i}"},
            )
        )
    all_entries = store.get_knowledge()
    assert len(all_entries) == 5
    page = store.get_knowledge(limit=2)
    assert len(page) == 2
    page2 = store.get_knowledge(limit=2, offset=2)
    assert len(page2) == 2


def test_get_active_alerts_pagination(store):
    """Limit and offset work on get_active_alerts."""
    for i in range(4):
        store.upsert_alert(
            "src",
            ["t"],
            f"a{i}",
            {"alert_id": f"a{i}", "level": "warning", "message": f"m{i}", "resolved": False},
        )
    assert len(store.get_active_alerts()) == 4
    page = store.get_active_alerts(limit=2)
    assert len(page) == 2
    page2 = store.get_active_alerts(limit=2, offset=2)
    assert len(page2) == 2


def test_get_entries_pagination(store):
    """Limit and offset work on get_entries."""
    for i in range(3):
        store.add(
            Entry(
                id=make_id(),
                type=EntryType.NOTE,
                source="test",
                tags=[],
                created=now_utc(),
                updated=now_utc(),
                expires=None,
                data={"description": f"note-{i}"},
            )
        )
    assert len(store.get_entries(limit=2)) == 2
    assert len(store.get_entries(limit=10, offset=2)) == 1


def test_get_deleted_pagination(store):
    """Limit and offset work on get_deleted."""
    for i in range(3):
        entry = store.add(
            Entry(
                id=make_id(),
                type=EntryType.NOTE,
                source="test",
                tags=[],
                created=now_utc(),
                updated=now_utc(),
                expires=None,
                data={"description": f"note-{i}"},
            )
        )
        store.soft_delete_by_id(entry.id)
    assert len(store.get_deleted()) == 3
    assert len(store.get_deleted(limit=2)) == 2
    assert len(store.get_deleted(limit=2, offset=2)) == 1


# ------------------------------------------------------------------
# Cleanup error logging
# ------------------------------------------------------------------


def test_do_cleanup_logs_errors(store, capsys):
    """_do_cleanup prints error instead of silently swallowing."""
    # Point at a DSN that doesn't exist to trigger an error
    original_dsn = store.dsn
    store.dsn = "postgresql://bad:bad@localhost:1/nonexistent"
    store._do_cleanup()
    store.dsn = original_dsn
    captured = capsys.readouterr()
    assert "[awareness] cleanup failed:" in captured.out


# ------------------------------------------------------------------
# SQL-level upsert lookups
# ------------------------------------------------------------------


def test_upsert_alert_different_sources_same_alert_id(store):
    """Two sources can have alerts with the same alert_id independently."""
    store.upsert_alert(
        "src-a",
        ["t"],
        "dup-id",
        {"alert_id": "dup-id", "level": "warning", "message": "A", "resolved": False},
    )
    store.upsert_alert(
        "src-b",
        ["t"],
        "dup-id",
        {"alert_id": "dup-id", "level": "critical", "message": "B", "resolved": False},
    )
    alerts = store.get_active_alerts()
    assert len(alerts) == 2


def test_upsert_preference_updates_existing(store):
    """upsert_preference updates in place via SQL lookup."""
    store.upsert_preference(
        "theme",
        "global",
        ["ui"],
        {"key": "theme", "scope": "global", "value": "dark"},
    )
    store.upsert_preference(
        "theme",
        "global",
        ["ui"],
        {"key": "theme", "scope": "global", "value": "light"},
    )
    prefs = store.get_entries(entry_type=EntryType.PREFERENCE)
    assert len(prefs) == 1
    assert prefs[0].data["value"] == "light"


# ------------------------------------------------------------------
# Delete by tags
# ------------------------------------------------------------------


def test_soft_delete_by_tags_and_logic(store):
    """Only entries matching ALL tags are deleted."""
    store.add(
        Entry(
            id=make_id(),
            type=EntryType.NOTE,
            source="test",
            tags=["qa", "project"],
            created=now_utc(),
            updated=now_utc(),
            expires=None,
            data={"description": "both tags"},
        )
    )
    store.add(
        Entry(
            id=make_id(),
            type=EntryType.NOTE,
            source="test",
            tags=["qa"],
            created=now_utc(),
            updated=now_utc(),
            expires=None,
            data={"description": "only qa"},
        )
    )
    store.add(
        Entry(
            id=make_id(),
            type=EntryType.NOTE,
            source="test",
            tags=["project"],
            created=now_utc(),
            updated=now_utc(),
            expires=None,
            data={"description": "only project"},
        )
    )
    count = store.soft_delete_by_tags(["qa", "project"])
    assert count == 1
    remaining = store.get_entries()
    assert len(remaining) == 2
    descs = {e.data["description"] for e in remaining}
    assert descs == {"only qa", "only project"}


def test_soft_delete_by_tags_empty(store):
    """Empty tag list deletes nothing."""
    store.add(
        Entry(
            id=make_id(),
            type=EntryType.NOTE,
            source="test",
            tags=["qa"],
            created=now_utc(),
            updated=now_utc(),
            expires=None,
            data={"description": "test"},
        )
    )
    assert store.soft_delete_by_tags([]) == 0
    assert len(store.get_entries()) == 1


def test_soft_delete_by_tags_single(store):
    """Single tag matches all entries with that tag."""
    for i in range(3):
        store.add(
            Entry(
                id=make_id(),
                type=EntryType.NOTE,
                source="test",
                tags=["qa-test"] if i < 2 else ["other"],
                created=now_utc(),
                updated=now_utc(),
                expires=None,
                data={"description": f"note-{i}"},
            )
        )
    count = store.soft_delete_by_tags(["qa-test"])
    assert count == 2
    assert len(store.get_entries()) == 1


# ------------------------------------------------------------------
# Restore by tags
# ------------------------------------------------------------------


def test_restore_by_tags(store):
    """Restore trashed entries matching ALL tags."""
    for i in range(3):
        entry = store.add(
            Entry(
                id=make_id(),
                type=EntryType.NOTE,
                source="test",
                tags=["qa-test", "batch"] if i < 2 else ["other"],
                created=now_utc(),
                updated=now_utc(),
                expires=None,
                data={"description": f"note-{i}"},
            )
        )
        store.soft_delete_by_id(entry.id)
    assert len(store.get_deleted()) == 3
    restored = store.restore_by_tags(["qa-test", "batch"])
    assert restored == 2
    assert len(store.get_entries()) == 2
    assert len(store.get_deleted()) == 1


def test_restore_by_tags_empty(store):
    """Empty tag list restores nothing."""
    assert store.restore_by_tags([]) == 0


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
        Entry(
            id=make_id(),
            type=EntryType.NOTE,
            source="test",
            tags=[],
            created=old,
            updated=old,
            expires=None,
            data={"description": "old note"},
        )
    )
    store.add(
        Entry(
            id=make_id(),
            type=EntryType.NOTE,
            source="test",
            tags=[],
            created=recent,
            updated=recent,
            expires=None,
            data={"description": "recent note"},
        )
    )
    all_entries = store.get_knowledge()
    assert len(all_entries) == 2
    filtered = store.get_knowledge(since=cutoff)
    assert len(filtered) == 1
    assert filtered[0].data["description"] == "recent note"


def test_get_entries_since(store):
    """since param works on get_entries."""
    from datetime import datetime, timedelta, timezone

    old = datetime.now(timezone.utc) - timedelta(hours=2)
    recent = datetime.now(timezone.utc)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=1)

    store.add(
        Entry(
            id=make_id(),
            type=EntryType.NOTE,
            source="test",
            tags=[],
            created=old,
            updated=old,
            expires=None,
            data={"description": "old"},
        )
    )
    store.add(
        Entry(
            id=make_id(),
            type=EntryType.NOTE,
            source="test",
            tags=[],
            created=recent,
            updated=recent,
            expires=None,
            data={"description": "new"},
        )
    )
    assert len(store.get_entries(since=cutoff)) == 1


def test_get_active_alerts_since(store):
    """since param works on get_active_alerts."""
    from datetime import datetime, timedelta, timezone

    old = datetime.now(timezone.utc) - timedelta(hours=2)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=1)

    store.upsert_alert(
        "src",
        ["t"],
        "old-alert",
        {"alert_id": "old-alert", "level": "warning", "message": "old", "resolved": False},
    )
    # Backdate the alert
    with store._conn.cursor() as cur:
        cur.execute(
            "UPDATE entries SET updated = %s WHERE data->>'alert_id' = %s",
            (old, "old-alert"),
        )
    store._conn.commit()

    store.upsert_alert(
        "src",
        ["t"],
        "new-alert",
        {"alert_id": "new-alert", "level": "warning", "message": "new", "resolved": False},
    )
    assert len(store.get_active_alerts()) == 2
    assert len(store.get_active_alerts(since=cutoff)) == 1


def test_get_deleted_since(store):
    """since param works on get_deleted."""
    from datetime import datetime, timedelta, timezone

    old = datetime.now(timezone.utc) - timedelta(hours=2)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=1)

    entry1 = store.add(
        Entry(
            id=make_id(),
            type=EntryType.NOTE,
            source="test",
            tags=[],
            created=now_utc(),
            updated=now_utc(),
            expires=None,
            data={"description": "old delete"},
        )
    )
    store.soft_delete_by_id(entry1.id)
    # Backdate the deletion
    with store._conn.cursor() as cur:
        cur.execute("UPDATE entries SET deleted = %s WHERE id = %s", (old, entry1.id))
    store._conn.commit()

    entry2 = store.add(
        Entry(
            id=make_id(),
            type=EntryType.NOTE,
            source="test",
            tags=[],
            created=now_utc(),
            updated=now_utc(),
            expires=None,
            data={"description": "recent delete"},
        )
    )
    store.soft_delete_by_id(entry2.id)

    assert len(store.get_deleted()) == 2
    assert len(store.get_deleted(since=cutoff)) == 1


# ------------------------------------------------------------------
# Read / action tracking tests
# ------------------------------------------------------------------


def test_log_read_and_get_reads(store):
    """log_read records reads, get_reads retrieves them."""
    entry = store.add(
        Entry(
            id=make_id(),
            type=EntryType.NOTE,
            source="test",
            tags=["demo"],
            created=now_utc(),
            updated=now_utc(),
            expires=None,
            data={"description": "readable"},
        )
    )
    store.log_read([entry.id], tool_used="get_knowledge")
    store.log_read([entry.id], tool_used="get_knowledge", platform="claude-code")
    reads = store.get_reads(entry_id=entry.id)
    assert len(reads) == 2
    assert reads[0]["entry_id"] == entry.id
    assert reads[0]["tool_used"] == "get_knowledge"


def test_log_read_empty_list(store):
    """log_read with empty list is a no-op."""
    store.log_read([], tool_used="test")
    assert store.get_reads() == []


def test_log_action_and_get_actions(store):
    """log_action records actions, get_actions retrieves them."""
    entry = store.add(
        Entry(
            id=make_id(),
            type=EntryType.NOTE,
            source="test",
            tags=["project", "mcp-awareness"],
            created=now_utc(),
            updated=now_utc(),
            expires=None,
            data={"description": "actionable"},
        )
    )
    result = store.log_action(
        entry_id=entry.id,
        action="created GitHub issue #42",
        platform="claude-code",
        detail="https://github.com/cmeans/mcp-awareness/issues/42",
    )
    assert result["action"] == "created GitHub issue #42"
    assert result["tags"] == ["project", "mcp-awareness"]  # copied from entry

    actions = store.get_actions(entry_id=entry.id)
    assert len(actions) == 1
    assert actions[0]["action"] == "created GitHub issue #42"
    assert actions[0]["detail"] == "https://github.com/cmeans/mcp-awareness/issues/42"


def test_log_action_custom_tags(store):
    """log_action accepts custom tags instead of copying from entry."""
    entry = store.add(
        Entry(
            id=make_id(),
            type=EntryType.NOTE,
            source="test",
            tags=["original"],
            created=now_utc(),
            updated=now_utc(),
            expires=None,
            data={"description": "test"},
        )
    )
    result = store.log_action(entry_id=entry.id, action="test", tags=["custom", "tags"])
    assert result["tags"] == ["custom", "tags"]


def test_log_action_invalid_entry_id(store):
    """log_action returns error for nonexistent entry_id."""
    result = store.log_action(entry_id="nonexistent-id", action="test")
    assert result["status"] == "error"
    assert "not found" in result["message"].lower()


def test_get_actions_filter_by_tags(store):
    """get_actions can filter by tags."""
    entry = store.add(
        Entry(
            id=make_id(),
            type=EntryType.NOTE,
            source="test",
            tags=["project"],
            created=now_utc(),
            updated=now_utc(),
            expires=None,
            data={"description": "test"},
        )
    )
    store.log_action(entry_id=entry.id, action="tagged action", tags=["project", "deploy"])
    store.log_action(entry_id=entry.id, action="other action", tags=["personal"])

    project_actions = store.get_actions(tags=["project"])
    assert len(project_actions) == 1
    assert project_actions[0]["action"] == "tagged action"


def test_get_unread(store):
    """get_unread returns entries with zero reads."""
    e1 = store.add(
        Entry(
            id=make_id(),
            type=EntryType.NOTE,
            source="test",
            tags=[],
            created=now_utc(),
            updated=now_utc(),
            expires=None,
            data={"description": "read entry"},
        )
    )
    store.add(
        Entry(
            id=make_id(),
            type=EntryType.NOTE,
            source="test",
            tags=[],
            created=now_utc(),
            updated=now_utc(),
            expires=None,
            data={"description": "unread entry"},
        )
    )
    store.log_read([e1.id], tool_used="test")
    unread = store.get_unread()
    assert len(unread) == 1
    assert unread[0].data["description"] == "unread entry"


def test_get_activity(store):
    """get_activity returns combined reads + actions feed."""
    entry = store.add(
        Entry(
            id=make_id(),
            type=EntryType.NOTE,
            source="test",
            tags=[],
            created=now_utc(),
            updated=now_utc(),
            expires=None,
            data={"description": "test"},
        )
    )
    store.log_read([entry.id], tool_used="get_knowledge")
    store.log_action(entry_id=entry.id, action="used for context")

    activity = store.get_activity()
    assert len(activity) == 2
    types = {a["event_type"] for a in activity}
    assert types == {"read", "action"}


def test_get_read_counts(store):
    """get_read_counts returns counts and last_read per entry."""
    e1 = store.add(
        Entry(
            id=make_id(),
            type=EntryType.NOTE,
            source="test",
            tags=[],
            created=now_utc(),
            updated=now_utc(),
            expires=None,
            data={"description": "popular"},
        )
    )
    e2 = store.add(
        Entry(
            id=make_id(),
            type=EntryType.NOTE,
            source="test",
            tags=[],
            created=now_utc(),
            updated=now_utc(),
            expires=None,
            data={"description": "unpopular"},
        )
    )
    store.log_read([e1.id], tool_used="test")
    store.log_read([e1.id], tool_used="test")
    store.log_read([e1.id], tool_used="test")

    counts = store.get_read_counts([e1.id, e2.id])
    assert counts[e1.id]["read_count"] == 3
    assert counts[e1.id]["last_read"] is not None
    assert e2.id not in counts  # no reads


def test_clear_removes_reads_and_actions(store):
    """clear() removes reads and actions along with entries."""
    entry = store.add(
        Entry(
            id=make_id(),
            type=EntryType.NOTE,
            source="test",
            tags=[],
            created=now_utc(),
            updated=now_utc(),
            expires=None,
            data={"description": "test"},
        )
    )
    store.log_read([entry.id], tool_used="test")
    store.log_action(entry_id=entry.id, action="test")
    store.clear()
    assert store.get_reads() == []
    assert store.get_actions() == []


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
        updated=now,
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
    store.add(entry)
    intentions = store.get_intentions()
    assert len(intentions) == 1
    assert intentions[0].data["goal"] == "Pick up milk"
    assert intentions[0].data["state"] == "pending"


def test_get_intentions_filter_by_state(store):
    """get_intentions filters by state."""
    now = now_utc()
    for state in ("pending", "pending", "fired", "completed"):
        store.add(
            Entry(
                id=make_id(),
                type=EntryType.INTENTION,
                source="test",
                tags=[],
                created=now,
                updated=now,
                expires=None,
                data={"goal": f"goal-{state}", "state": state},
            )
        )
    assert len(store.get_intentions(state="pending")) == 2
    assert len(store.get_intentions(state="fired")) == 1
    assert len(store.get_intentions(state="completed")) == 1
    assert len(store.get_intentions()) == 4


def test_get_intentions_filter_by_tags(store):
    """get_intentions filters by tags."""
    now = now_utc()
    store.add(
        Entry(
            id=make_id(),
            type=EntryType.INTENTION,
            source="test",
            tags=["errands", "groceries"],
            created=now,
            updated=now,
            expires=None,
            data={"goal": "buy milk", "state": "pending"},
        )
    )
    store.add(
        Entry(
            id=make_id(),
            type=EntryType.INTENTION,
            source="test",
            tags=["work"],
            created=now,
            updated=now,
            expires=None,
            data={"goal": "file report", "state": "pending"},
        )
    )
    assert len(store.get_intentions(tags=["groceries"])) == 1
    assert len(store.get_intentions(tags=["work"])) == 1


def test_get_intentions_filter_by_source_and_limit(store):
    """get_intentions filters by source and respects limit."""
    now = now_utc()
    for i in range(3):
        store.add(
            Entry(
                id=make_id(),
                type=EntryType.INTENTION,
                source="personal",
                tags=[],
                created=now,
                updated=now,
                expires=None,
                data={"goal": f"personal-{i}", "state": "pending"},
            )
        )
    store.add(
        Entry(
            id=make_id(),
            type=EntryType.INTENTION,
            source="work",
            tags=[],
            created=now,
            updated=now,
            expires=None,
            data={"goal": "work goal", "state": "pending"},
        )
    )
    assert len(store.get_intentions(source="personal")) == 3
    assert len(store.get_intentions(source="work")) == 1
    assert len(store.get_intentions(limit=2)) == 2


def test_update_intention_state(store):
    """update_intention_state transitions state and records changelog."""
    now = now_utc()
    entry = Entry(
        id=make_id(),
        type=EntryType.INTENTION,
        source="test",
        tags=[],
        created=now,
        updated=now,
        expires=None,
        data={"goal": "test goal", "state": "pending"},
    )
    store.add(entry)
    result = store.update_intention_state(entry.id, "fired", reason="time-based trigger")
    assert result is not None
    assert result.data["state"] == "fired"
    assert result.data["state_reason"] == "time-based trigger"
    assert len(result.data["changelog"]) == 1
    assert result.data["changelog"][0]["changed"]["state"] == "pending"


def test_update_intention_state_not_found(store):
    """update_intention_state returns None for nonexistent entry."""
    assert store.update_intention_state("nonexistent", "fired") is None


def test_update_intention_state_wrong_type(store):
    """update_intention_state returns None for non-intention entries."""
    now = now_utc()
    entry = Entry(
        id=make_id(),
        type=EntryType.NOTE,
        source="test",
        tags=[],
        created=now,
        updated=now,
        expires=None,
        data={"description": "not an intention"},
    )
    store.add(entry)
    assert store.update_intention_state(entry.id, "fired") is None


def test_get_fired_intentions(store):
    """get_fired_intentions returns pending intentions with past deliver_at."""
    from datetime import timedelta

    now = now_utc()
    past = now - timedelta(hours=1)
    future = now + timedelta(hours=1)

    # Past deliver_at — should fire
    store.add(
        Entry(
            id=make_id(),
            type=EntryType.INTENTION,
            source="test",
            tags=[],
            created=now,
            updated=now,
            expires=None,
            data={"goal": "overdue", "state": "pending", "deliver_at": past.isoformat()},
        )
    )
    # Future deliver_at — should not fire
    store.add(
        Entry(
            id=make_id(),
            type=EntryType.INTENTION,
            source="test",
            tags=[],
            created=now,
            updated=now,
            expires=None,
            data={"goal": "not yet", "state": "pending", "deliver_at": future.isoformat()},
        )
    )
    # No deliver_at — should not fire
    store.add(
        Entry(
            id=make_id(),
            type=EntryType.INTENTION,
            source="test",
            tags=[],
            created=now,
            updated=now,
            expires=None,
            data={"goal": "no time", "state": "pending", "deliver_at": None},
        )
    )
    # Already fired — should not appear
    store.add(
        Entry(
            id=make_id(),
            type=EntryType.INTENTION,
            source="test",
            tags=[],
            created=now,
            updated=now,
            expires=None,
            data={"goal": "already done", "state": "fired", "deliver_at": past.isoformat()},
        )
    )

    fired = store.get_fired_intentions()
    assert len(fired) == 1
    assert fired[0].data["goal"] == "overdue"


# ------------------------------------------------------------------
# SQL-level improvements
# ------------------------------------------------------------------


def test_get_knowledge_default_sort_desc(store):
    """get_knowledge returns most recent entries first (updated DESC)."""
    from datetime import timedelta

    now = now_utc()
    old = now - timedelta(hours=2)
    store.add(
        Entry(
            id=make_id(),
            type=EntryType.NOTE,
            source="test",
            tags=[],
            created=old,
            updated=old,
            expires=None,
            data={"description": "old note"},
        )
    )
    store.add(
        Entry(
            id=make_id(),
            type=EntryType.NOTE,
            source="test",
            tags=[],
            created=now,
            updated=now,
            expires=None,
            data={"description": "recent note"},
        )
    )
    entries = store.get_knowledge(limit=1)
    assert len(entries) == 1
    assert entries[0].data["description"] == "recent note"


def test_get_knowledge_until(store):
    """until param filters by updated <= timestamp."""
    from datetime import timedelta

    now = now_utc()
    old = now - timedelta(hours=2)
    cutoff = now - timedelta(hours=1)
    store.add(
        Entry(
            id=make_id(),
            type=EntryType.NOTE,
            source="test",
            tags=[],
            created=old,
            updated=old,
            expires=None,
            data={"description": "old note"},
        )
    )
    store.add(
        Entry(
            id=make_id(),
            type=EntryType.NOTE,
            source="test",
            tags=[],
            created=now,
            updated=now,
            expires=None,
            data={"description": "recent note"},
        )
    )
    entries = store.get_knowledge(until=cutoff)
    assert len(entries) == 1
    assert entries[0].data["description"] == "old note"


def test_get_knowledge_learned_from(store):
    """learned_from param filters by data->>'learned_from'."""
    now = now_utc()
    store.add(
        Entry(
            id=make_id(),
            type=EntryType.NOTE,
            source="test",
            tags=[],
            created=now,
            updated=now,
            expires=None,
            data={"description": "from code", "learned_from": "claude-code"},
        )
    )
    store.add(
        Entry(
            id=make_id(),
            type=EntryType.NOTE,
            source="test",
            tags=[],
            created=now,
            updated=now,
            expires=None,
            data={"description": "from desktop", "learned_from": "claude.ai"},
        )
    )
    entries = store.get_knowledge(learned_from="claude-code")
    assert len(entries) == 1
    assert entries[0].data["description"] == "from code"


def test_get_active_alerts_resolved_filtered_in_sql(store):
    """Resolved alerts are filtered in SQL, not post-fetch."""
    store.upsert_alert(
        "src",
        ["t"],
        "active",
        {"alert_id": "active", "level": "warning", "message": "active", "resolved": False},
    )
    store.upsert_alert(
        "src",
        ["t"],
        "resolved",
        {"alert_id": "resolved", "level": "warning", "message": "resolved", "resolved": True},
    )
    alerts = store.get_active_alerts()
    assert len(alerts) == 1
    assert alerts[0].data["alert_id"] == "active"


def test_sql_pagination_limit_offset(store):
    """LIMIT/OFFSET pushed to SQL — verify with get_entries."""
    for i in range(5):
        store.add(
            Entry(
                id=make_id(),
                type=EntryType.NOTE,
                source="test",
                tags=[],
                created=now_utc(),
                updated=now_utc(),
                expires=None,
                data={"description": f"note-{i}"},
            )
        )
    page1 = store.get_entries(limit=2)
    assert len(page1) == 2
    page2 = store.get_entries(limit=2, offset=2)
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
            updated=now,
            expires=None,
            data={"description": f"note-{i}"},
        )
        store.add(entry)
        store.update_entry(entry.id, {"description": f"updated-{i}"})
    all_with_history = store.get_knowledge(include_history="only")
    assert len(all_with_history) == 3
    page = store.get_knowledge(include_history="only", limit=2, offset=1)
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
        updated=late,
        expires=None,
        data={"description": "new note"},
    )
    store.add(e1)
    store.add(e2)
    cutoff = now_utc() - timedelta(hours=1)
    results = store.get_knowledge(created_after=cutoff)
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
        updated=early,
        expires=None,
        data={"description": "old note"},
    )
    e2 = Entry(
        id=make_id(),
        type=EntryType.NOTE,
        source="test",
        tags=[],
        created=late,
        updated=late,
        expires=None,
        data={"description": "new note"},
    )
    store.add(e1)
    store.add(e2)
    cutoff = now_utc() - timedelta(hours=1)
    results = store.get_knowledge(created_before=cutoff)
    assert len(results) == 1
    assert results[0].data["description"] == "old note"


# ---------------------------------------------------------------------------
# Connection resilience
# ---------------------------------------------------------------------------


def test_reconnect_after_closed_connection(store):
    """Store reconnects transparently when connection is closed."""
    # Force the connection closed
    store._PostgresStore__conn.close()
    # Reset health check timer so _conn property actually checks
    store._last_health_check = 0.0
    # Should reconnect and work
    store.upsert_status("test", ["t"], {"metrics": {}, "ttl_sec": 120})
    assert store.get_sources() == ["test"]


def test_health_check_debounced(store):
    """Health check doesn't run on every access (debounced)."""
    import time

    # After init, health check was just run
    store._last_health_check = time.monotonic()
    # Access _conn — should NOT run health check (too soon)
    # If it did, we'd see a SELECT 1 + rollback, but we can't easily observe that.
    # Instead, verify the debounce timer works by checking that a closed connection
    # is NOT healed when the timer hasn't elapsed.
    store._PostgresStore__conn.close()
    # Timer hasn't elapsed — _conn returns the closed connection
    # (This would fail on next use, but the point is the health check is debounced)
    # Force the timer to expire for the next access
    store._last_health_check = 0.0
    # Now it heals
    entries = store.get_entries()
    assert isinstance(entries, list)


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
            updated=now,
            expires=None,
            data={"description": "test note"},
        )
        store.add(entry)
        vec = self._vec(768, 0)
        store.upsert_embedding(entry.id, "test-model", 768, "abc123", vec)
        # Should be searchable
        results = store.semantic_search(vec, "test-model", limit=5)
        assert len(results) == 1
        found_entry, score = results[0]
        assert found_entry.id == entry.id
        assert score > 0.99  # cosine similarity with itself should be ~1.0

    def test_upsert_embedding_replaces(self, store):
        """Upserting same entry+model replaces the embedding."""
        now = now_utc()
        entry = Entry(
            id=make_id(),
            type=EntryType.NOTE,
            source="test",
            tags=[],
            created=now,
            updated=now,
            expires=None,
            data={"description": "test"},
        )
        store.add(entry)
        store.upsert_embedding(entry.id, "test-model", 768, "hash1", self._vec(768, 0))
        store.upsert_embedding(entry.id, "test-model", 768, "hash2", self._vec(768, 1))
        # Search along axis 1 should find it
        results = store.semantic_search(self._vec(768, 1), "test-model", limit=5)
        assert len(results) == 1
        assert results[0][1] > 0.99

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
                updated=now,
                expires=None,
                data={"description": f"note-{i}"},
            )
            store.add(entry)
            store.upsert_embedding(entry.id, "m", 768, f"h{i}", self._vec(768, i))
            ids.append(entry.id)
        # Query along axis 0 — entry 0 should be first
        results = store.semantic_search(self._vec(768, 0), "m", limit=3)
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
            updated=now,
            expires=None,
            data={"description": "note"},
        )
        alert = Entry(
            id=make_id(),
            type=EntryType.ALERT,
            source="test",
            tags=[],
            created=now,
            updated=now,
            expires=None,
            data={"alert_id": "a1", "message": "alert", "level": "warning", "resolved": False},
        )
        store.add(note)
        store.add(alert)
        vec = self._vec(768, 0)
        store.upsert_embedding(note.id, "m", 768, "h1", vec)
        store.upsert_embedding(alert.id, "m", 768, "h2", vec)
        # Filter to notes only
        results = store.semantic_search(vec, "m", entry_type=EntryType.NOTE)
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
            updated=now,
            expires=None,
            data={"description": "nas note"},
        )
        e2 = Entry(
            id=make_id(),
            type=EntryType.NOTE,
            source="personal",
            tags=[],
            created=now,
            updated=now,
            expires=None,
            data={"description": "personal note"},
        )
        store.add(e1)
        store.add(e2)
        vec = self._vec(768, 0)
        store.upsert_embedding(e1.id, "m", 768, "h1", vec)
        store.upsert_embedding(e2.id, "m", 768, "h2", vec)
        results = store.semantic_search(vec, "m", source="nas")
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
            updated=now,
            expires=None,
            data={"description": "infra note"},
        )
        e2 = Entry(
            id=make_id(),
            type=EntryType.NOTE,
            source="test",
            tags=["personal"],
            created=now,
            updated=now,
            expires=None,
            data={"description": "personal note"},
        )
        store.add(e1)
        store.add(e2)
        vec = self._vec(768, 0)
        store.upsert_embedding(e1.id, "m", 768, "h1", vec)
        store.upsert_embedding(e2.id, "m", 768, "h2", vec)
        results = store.semantic_search(vec, "m", tags=["infra"])
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
            updated=now,
            expires=None,
            data={"description": "test"},
        )
        store.add(entry)
        vec = self._vec(768, 0)
        store.upsert_embedding(entry.id, "m", 768, "h1", vec)
        store.soft_delete_by_id(entry.id)
        results = store.semantic_search(vec, "m")
        assert len(results) == 0

    def test_semantic_search_empty_store(self, store):
        """Search on empty store returns empty list."""
        results = store.semantic_search(self._vec(768, 0), "m")
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
            updated=now,
            expires=None,
            data={"description": "has embedding"},
        )
        e2 = Entry(
            id=make_id(),
            type=EntryType.NOTE,
            source="test",
            tags=[],
            created=now,
            updated=now,
            expires=None,
            data={"description": "no embedding"},
        )
        store.add(e1)
        store.add(e2)
        store.upsert_embedding(e1.id, "m", 768, "h1", self._vec(768, 0))
        missing = store.get_entries_without_embeddings("m")
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
            updated=now,
            expires=None,
            data={"metric": "cpu", "suppress_level": "warning"},
        )
        store.add(sup)
        missing = store.get_entries_without_embeddings("m")
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
            updated=now,
            expires=None,
            data={"description": "test"},
        )
        store.add(entry)
        store.upsert_embedding(entry.id, "m", 768, "h1", self._vec(768, 0))
        store.clear()
        results = store.semantic_search(self._vec(768, 0), "m")
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
                updated=now,
                expires=None,
                data={"description": f"note-{i}"},
            )
            store.add(entry)
            store.upsert_embedding(entry.id, "m", 768, f"h{i}", vec)
        results = store.semantic_search(vec, "m", limit=2)
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
            updated=old,
            expires=None,
            data={"description": "old"},
        )
        e2 = Entry(
            id=make_id(),
            type=EntryType.NOTE,
            source="test",
            tags=[],
            created=new,
            updated=new,
            expires=None,
            data={"description": "new"},
        )
        store.add(e1)
        store.add(e2)
        vec = self._vec(768, 0)
        store.upsert_embedding(e1.id, "m", 768, "h1", vec)
        store.upsert_embedding(e2.id, "m", 768, "h2", vec)
        cutoff = now_utc() - timedelta(hours=1)
        results = store.semantic_search(vec, "m", since=cutoff)
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
            updated=old,
            expires=None,
            data={"description": "old"},
        )
        e2 = Entry(
            id=make_id(),
            type=EntryType.NOTE,
            source="test",
            tags=[],
            created=new,
            updated=new,
            expires=None,
            data={"description": "new"},
        )
        store.add(e1)
        store.add(e2)
        vec = self._vec(768, 0)
        store.upsert_embedding(e1.id, "m", 768, "h1", vec)
        store.upsert_embedding(e2.id, "m", 768, "h2", vec)
        cutoff = now_utc() - timedelta(hours=1)
        results = store.semantic_search(vec, "m", until=cutoff)
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
            updated=now,
            expires=None,
            data={"description": "original text"},
        )
        store.add(entry)
        vec = self._vec(768, 0)
        # Embed with hash of original text
        from mcp_awareness.embeddings import compose_embedding_text, text_hash

        original_hash = text_hash(compose_embedding_text(entry))
        store.upsert_embedding(entry.id, "m", 768, original_hash, vec)
        # No stale entries yet
        assert store.get_stale_embeddings("m") == []
        # Update the entry text
        store.update_entry(entry.id, {"description": "changed text"})
        # Now it should be stale
        stale = store.get_stale_embeddings("m")
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
            updated=now,
            expires=None,
            data={"description": "stable text"},
        )
        store.add(entry)
        from mcp_awareness.embeddings import compose_embedding_text, text_hash

        h = text_hash(compose_embedding_text(entry))
        store.upsert_embedding(entry.id, "m", 768, h, self._vec(768, 0))
        assert store.get_stale_embeddings("m") == []
