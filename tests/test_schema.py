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

"""Tests for schema validation, entry creation, TTL expiry, and serialization."""

from datetime import datetime, timedelta, timezone

from mcp_awareness.schema import (
    Entry,
    EntryType,
    make_id,
    now_utc,
    parse_iso,
    severity_rank,
    validate_entry_data,
)


def test_parse_iso_with_z_suffix():
    dt = parse_iso("2026-03-24T12:00:00Z")
    assert dt.tzinfo is not None
    assert dt.utcoffset().total_seconds() == 0


def test_parse_iso_with_offset():
    dt = parse_iso("2026-03-24T12:00:00-05:00")
    assert dt.tzinfo is not None
    assert dt.utcoffset().total_seconds() == -5 * 3600


def test_parse_iso_naive_gets_utc():
    """Naive datetime (no timezone) should be treated as UTC."""
    dt = parse_iso("2026-03-24T12:00:00")
    assert dt.tzinfo is not None
    assert dt.utcoffset().total_seconds() == 0


def test_parse_iso_invalid_raises():
    import pytest

    with pytest.raises(ValueError):
        parse_iso("not-a-date")


def test_entry_roundtrip():
    now = now_utc()
    entry = Entry(
        id=make_id(),
        type=EntryType.STATUS,
        source="test-source",
        tags=["infra", "test"],
        created=now,
        updated=now,
        expires=None,
        data={"metrics": {"cpu": 50}},
    )
    d = entry.to_dict()
    restored = Entry.from_dict(d)
    assert restored.id == entry.id
    assert restored.type == EntryType.STATUS
    assert restored.source == "test-source"
    assert restored.tags == ["infra", "test"]
    assert restored.data == {"metrics": {"cpu": 50}}


def test_entry_type_serialization():
    now = now_utc()
    for et in EntryType:
        entry = Entry(
            id="x",
            type=et,
            source="s",
            tags=[],
            created=now,
            updated=now,
            expires=None,
            data={},
        )
        d = entry.to_dict()
        assert d["type"] == et.value
        assert Entry.from_dict(d).type == et


def test_is_expired_none():
    entry = Entry(
        id="x",
        type=EntryType.ALERT,
        source="s",
        tags=[],
        created=now_utc(),
        updated=now_utc(),
        expires=None,
        data={},
    )
    assert not entry.is_expired()


def test_is_expired_future():
    future = datetime.now(timezone.utc) + timedelta(hours=1)
    entry = Entry(
        id="x",
        type=EntryType.SUPPRESSION,
        source="s",
        tags=[],
        created=now_utc(),
        updated=now_utc(),
        expires=future,
        data={},
    )
    assert not entry.is_expired()


def test_is_expired_past():
    past = datetime.now(timezone.utc) - timedelta(hours=1)
    entry = Entry(
        id="x",
        type=EntryType.SUPPRESSION,
        source="s",
        tags=[],
        created=now_utc(),
        updated=now_utc(),
        expires=past,
        data={},
    )
    assert entry.is_expired()


def test_is_stale_within_ttl():
    now = now_utc()
    entry = Entry(
        id="x",
        type=EntryType.STATUS,
        source="s",
        tags=[],
        created=now,
        updated=now,
        expires=None,
        data={"ttl_sec": 120},
    )
    assert not entry.is_stale()


def test_is_stale_expired_ttl():
    old = datetime.now(timezone.utc) - timedelta(seconds=300)
    entry = Entry(
        id="x",
        type=EntryType.STATUS,
        source="s",
        tags=[],
        created=old,
        updated=old,
        expires=None,
        data={"ttl_sec": 120},
    )
    assert entry.is_stale()


def test_is_stale_non_status():
    old = datetime.now(timezone.utc) - timedelta(seconds=300)
    entry = Entry(
        id="x",
        type=EntryType.ALERT,
        source="s",
        tags=[],
        created=old,
        updated=old,
        expires=None,
        data={"ttl_sec": 120},
    )
    assert not entry.is_stale()


def test_is_stale_no_ttl():
    old = datetime.now(timezone.utc) - timedelta(seconds=300)
    entry = Entry(
        id="x",
        type=EntryType.STATUS,
        source="s",
        tags=[],
        created=old,
        updated=old,
        expires=None,
        data={},
    )
    assert not entry.is_stale()


def test_severity_rank():
    assert severity_rank("critical") > severity_rank("warning")
    assert severity_rank("warning") > severity_rank("info")
    assert severity_rank("unknown") == -1


def test_validate_entry_data_valid():
    errors = validate_entry_data({"type": "status", "source": "s", "data": {}})
    assert errors == []


def test_validate_entry_data_missing_fields():
    errors = validate_entry_data({})
    assert len(errors) == 2
    assert any("type" in e for e in errors)
    assert any("source" in e for e in errors)


def test_validate_entry_data_invalid_type():
    errors = validate_entry_data({"type": "bogus", "source": "s"})
    assert any("Invalid type" in e for e in errors)


def test_validate_entry_data_bad_data_field():
    errors = validate_entry_data({"type": "status", "source": "s", "data": "string"})
    assert any("dict" in e for e in errors)


def test_validate_entry_data_bad_tags():
    errors = validate_entry_data({"type": "status", "source": "s", "tags": "string"})
    assert any("list" in e for e in errors)


# ------------------------------------------------------------------
# to_list_dict type-aware metadata
# ------------------------------------------------------------------


def test_to_list_dict_alert_uses_message():
    """Alerts use message as description fallback in list mode."""
    now = now_utc()
    entry = Entry(
        id=make_id(),
        type=EntryType.ALERT,
        source="nas",
        tags=["infra"],
        created=now,
        updated=now,
        expires=None,
        data={"alert_id": "a1", "level": "warning", "message": "CPU high"},
    )
    d = entry.to_list_dict()
    assert d["description"] == "CPU high"


def test_to_list_dict_intention_includes_goal_state():
    """Intentions include goal and state in list mode."""
    now = now_utc()
    entry = Entry(
        id=make_id(),
        type=EntryType.INTENTION,
        source="personal",
        tags=["errands"],
        created=now,
        updated=now,
        expires=None,
        data={"goal": "Pick up milk", "state": "pending"},
    )
    d = entry.to_list_dict()
    assert d["goal"] == "Pick up milk"
    assert d["state"] == "pending"
    assert "data" not in d
