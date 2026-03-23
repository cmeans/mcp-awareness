"""Tests for collation logic: suppression evaluation, pattern matching, briefing generation."""

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from mcp_awareness.collator import (
    compose_mention,
    compose_summary,
    generate_briefing,
    is_suppressed,
    matches_pattern,
)
from mcp_awareness.schema import Entry, EntryType, make_id, now_utc

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_alert(
    source="nas",
    level="warning",
    metric="cpu_pct",
    message="CPU high",
    alert_id="a1",
    alert_type="threshold",
    tags=None,
):
    now = now_utc()
    return Entry(
        id=make_id(),
        type=EntryType.ALERT,
        source=source,
        tags=tags or ["infra"],
        created=now,
        updated=now,
        expires=None,
        data={
            "alert_id": alert_id,
            "level": level,
            "alert_type": alert_type,
            "metric": metric,
            "message": message,
            "resolved": False,
        },
    )


def _make_suppression(
    source="nas",
    metric="cpu_pct",
    suppress_level="warning",
    escalation_override=True,
    hours_remaining=1,
    tags=None,
):
    now = datetime.now(timezone.utc)
    expires = now + timedelta(hours=hours_remaining)
    return Entry(
        id=make_id(),
        type=EntryType.SUPPRESSION,
        source=source,
        tags=tags or [],
        created=now,
        updated=now,
        expires=expires,
        data={
            "metric": metric,
            "suppress_level": suppress_level,
            "escalation_override": escalation_override,
        },
    )


def _make_pattern(source="nas", effect="suppress cpu_pct", conditions=None, tags=None):
    now = now_utc()
    return Entry(
        id=make_id(),
        type=EntryType.PATTERN,
        source=source,
        tags=tags or [],
        created=now,
        updated=now,
        expires=None,
        data={
            "description": "test",
            "conditions": conditions or {},
            "effect": effect,
            "learned_from": "test",
        },
    )


# ---------------------------------------------------------------------------
# is_suppressed
# ---------------------------------------------------------------------------


class TestIsSuppressed:
    def test_no_suppressions(self):
        alert = _make_alert()
        assert not is_suppressed(alert, [])

    def test_matching_suppression(self):
        alert = _make_alert(metric="cpu_pct")
        supp = _make_suppression(metric="cpu_pct")
        assert is_suppressed(alert, [supp])

    def test_non_matching_metric(self):
        alert = _make_alert(metric="cpu_pct")
        supp = _make_suppression(metric="disk_busy_pct")
        assert not is_suppressed(alert, [supp])

    def test_non_matching_source(self):
        alert = _make_alert(source="nas")
        supp = _make_suppression(source="ci", metric="cpu_pct")
        assert not is_suppressed(alert, [supp])

    def test_global_suppression_matches_any_source(self):
        alert = _make_alert(source="nas", metric="cpu_pct")
        supp = _make_suppression(source="", metric="cpu_pct")
        assert is_suppressed(alert, [supp])

    def test_none_metric_matches_any(self):
        alert = _make_alert(metric="cpu_pct")
        supp = _make_suppression(metric=None)
        assert is_suppressed(alert, [supp])

    def test_expired_suppression_ignored(self):
        alert = _make_alert(metric="cpu_pct")
        supp = _make_suppression(metric="cpu_pct", hours_remaining=-1)
        assert not is_suppressed(alert, [supp])

    def test_escalation_override_breaks_through(self):
        """Critical alert breaks through a warning-level suppression."""
        alert = _make_alert(level="critical", metric="cpu_pct")
        supp = _make_suppression(
            metric="cpu_pct",
            suppress_level="warning",
            escalation_override=True,
        )
        assert not is_suppressed(alert, [supp])

    def test_escalation_override_same_level_stays_suppressed(self):
        """Warning alert stays suppressed by a warning-level suppression."""
        alert = _make_alert(level="warning", metric="cpu_pct")
        supp = _make_suppression(
            metric="cpu_pct",
            suppress_level="warning",
            escalation_override=True,
        )
        assert is_suppressed(alert, [supp])

    def test_no_escalation_override_critical_stays_suppressed(self):
        """Without escalation override, critical stays suppressed too."""
        alert = _make_alert(level="critical", metric="cpu_pct")
        supp = _make_suppression(
            metric="cpu_pct",
            suppress_level="warning",
            escalation_override=False,
        )
        assert is_suppressed(alert, [supp])

    def test_tag_filtering(self):
        alert = _make_alert(tags=["infra", "nas"])
        supp = _make_suppression(metric=None, tags=["infra"])
        assert is_suppressed(alert, [supp])

    def test_tag_no_overlap(self):
        alert = _make_alert(tags=["infra", "nas"])
        supp = _make_suppression(metric=None, tags=["calendar"])
        assert not is_suppressed(alert, [supp])


# ---------------------------------------------------------------------------
# matches_pattern
# ---------------------------------------------------------------------------


class TestMatchesPattern:
    def test_no_patterns(self):
        alert = _make_alert()
        assert not matches_pattern(alert, [])

    def test_matching_pattern_no_conditions(self):
        alert = _make_alert(metric="cpu_pct")
        pattern = _make_pattern(effect="suppress cpu_pct")
        assert matches_pattern(alert, [pattern])

    def test_non_matching_effect(self):
        alert = _make_alert(metric="cpu_pct")
        pattern = _make_pattern(effect="suppress disk_busy_pct")
        assert not matches_pattern(alert, [pattern])

    def test_day_of_week_condition_matches(self):
        alert = _make_alert(alert_id="qbittorrent_stopped", message="qBittorrent stopped")
        pattern = _make_pattern(
            effect="suppress qbittorrent_stopped",
            conditions={"day_of_week": "friday"},
        )
        # Mock to Friday
        friday = datetime(2026, 3, 20, 12, 0, tzinfo=timezone.utc)  # 2026-03-20 is a Friday
        with patch("mcp_awareness.collator.datetime") as mock_dt:
            mock_dt.now.return_value = friday
            mock_dt.side_effect = lambda *a, **k: datetime(*a, **k)
            assert matches_pattern(alert, [pattern])

    def test_day_of_week_condition_no_match(self):
        alert = _make_alert(alert_id="qbittorrent_stopped", message="qBittorrent stopped")
        pattern = _make_pattern(
            effect="suppress qbittorrent_stopped",
            conditions={"day_of_week": "friday"},
        )
        # Mock to Monday
        monday = datetime(2026, 3, 16, 12, 0, tzinfo=timezone.utc)
        with patch("mcp_awareness.collator.datetime") as mock_dt:
            mock_dt.now.return_value = monday
            mock_dt.side_effect = lambda *a, **k: datetime(*a, **k)
            assert not matches_pattern(alert, [pattern])

    def test_hour_range_condition(self):
        alert = _make_alert(metric="cpu_pct")
        pattern = _make_pattern(
            effect="suppress cpu_pct",
            conditions={"hour_range": [9, 17]},
        )
        noon = datetime(2026, 3, 19, 12, 0, tzinfo=timezone.utc)
        with patch("mcp_awareness.collator.datetime") as mock_dt:
            mock_dt.now.return_value = noon
            mock_dt.side_effect = lambda *a, **k: datetime(*a, **k)
            assert matches_pattern(alert, [pattern])

    def test_hour_range_outside(self):
        alert = _make_alert(metric="cpu_pct")
        pattern = _make_pattern(
            effect="suppress cpu_pct",
            conditions={"hour_range": [9, 17]},
        )
        evening = datetime(2026, 3, 19, 20, 0, tzinfo=timezone.utc)
        with patch("mcp_awareness.collator.datetime") as mock_dt:
            mock_dt.now.return_value = evening
            mock_dt.side_effect = lambda *a, **k: datetime(*a, **k)
            assert not matches_pattern(alert, [pattern])

    def test_hour_range_overnight_inside(self):
        """Hour 23 is inside the overnight range [22, 6]."""
        alert = _make_alert(metric="cpu_pct")
        pattern = _make_pattern(
            effect="suppress cpu_pct",
            conditions={"hour_range": [22, 6]},
        )
        late_night = datetime(2026, 3, 19, 23, 0, tzinfo=timezone.utc)
        with patch("mcp_awareness.collator.datetime") as mock_dt:
            mock_dt.now.return_value = late_night
            mock_dt.side_effect = lambda *a, **k: datetime(*a, **k)
            assert matches_pattern(alert, [pattern])

    def test_hour_range_overnight_outside(self):
        """Hour 12 (noon) is outside the overnight range [22, 6]."""
        alert = _make_alert(metric="cpu_pct")
        pattern = _make_pattern(
            effect="suppress cpu_pct",
            conditions={"hour_range": [22, 6]},
        )
        noon = datetime(2026, 3, 19, 12, 0, tzinfo=timezone.utc)
        with patch("mcp_awareness.collator.datetime") as mock_dt:
            mock_dt.now.return_value = noon
            mock_dt.side_effect = lambda *a, **k: datetime(*a, **k)
            assert not matches_pattern(alert, [pattern])

    def test_effect_matches_message(self):
        alert = _make_alert(message="qBittorrent stopped — should always be running")
        pattern = _make_pattern(effect="suppress qbittorrent stopped")
        assert matches_pattern(alert, [pattern])


# ---------------------------------------------------------------------------
# compose_summary / compose_mention
# ---------------------------------------------------------------------------


class TestComposeSummary:
    def test_all_clear(self):
        briefing = {
            "attention_needed": False,
            "sources": {"nas": {"status": "ok"}, "ci": {"status": "ok"}},
        }
        summary = compose_summary(briefing)
        assert "All clear" in summary
        assert "2 sources" in summary

    def test_single_source(self):
        briefing = {
            "attention_needed": False,
            "sources": {"nas": {"status": "ok"}},
        }
        assert "1 source." in compose_summary(briefing)

    def test_warning(self):
        briefing = {
            "attention_needed": True,
            "sources": {"nas": {"status": "warning"}},
            "upcoming": [],
        }
        summary = compose_summary(briefing)
        assert "warning" in summary
        assert "nas" in summary

    def test_critical(self):
        briefing = {
            "attention_needed": True,
            "sources": {"nas": {"status": "critical"}},
            "upcoming": [],
        }
        assert "critical" in compose_summary(briefing)

    def test_stale(self):
        briefing = {
            "attention_needed": True,
            "sources": {"nas": {"status": "stale"}},
            "upcoming": [],
        }
        assert "stale" in compose_summary(briefing)


class TestComposeMention:
    def test_warning_mention(self):
        briefing = {
            "sources": {"nas": {"status": "warning", "headline": "CPU high"}},
            "upcoming": [],
        }
        mention = compose_mention(briefing)
        assert "FYI" in mention
        assert "CPU high" in mention

    def test_critical_mention(self):
        briefing = {
            "sources": {"nas": {"status": "critical", "headline": "CPU very high"}},
            "upcoming": [],
        }
        mention = compose_mention(briefing)
        assert "CRITICAL" in mention

    def test_upcoming_included(self):
        briefing = {
            "sources": {},
            "upcoming": [{"source": "gcal", "summary": "Meeting in 30 min"}],
        }
        mention = compose_mention(briefing)
        assert "Meeting in 30 min" in mention


# ---------------------------------------------------------------------------
# generate_briefing (integration with store)
# ---------------------------------------------------------------------------


class TestGenerateBriefing:
    # store fixture comes from conftest.py (testcontainers Postgres)

    def test_empty_store(self, store):
        briefing = generate_briefing(store)
        assert briefing["attention_needed"] is False
        assert briefing["active_alerts"] == 0
        assert briefing["sources"] == {}

    def test_all_clear(self, store):
        store.upsert_status("nas", ["infra"], {"metrics": {"cpu": 50}, "ttl_sec": 3600})
        store.upsert_status("ci", ["cicd"], {"metrics": {}, "ttl_sec": 3600})
        briefing = generate_briefing(store)
        assert briefing["attention_needed"] is False
        assert len(briefing["sources"]) == 2
        assert all(s["status"] == "ok" for s in briefing["sources"].values())
        assert "All clear" in briefing["summary"]

    def test_active_alert_triggers_attention(self, store):
        store.upsert_status("nas", ["infra"], {"metrics": {}, "ttl_sec": 3600})
        store.upsert_alert(
            "nas",
            ["infra"],
            "cpu-1",
            {
                "alert_id": "cpu-1",
                "level": "warning",
                "alert_type": "threshold",
                "message": "CPU at 96%",
                "resolved": False,
            },
        )
        briefing = generate_briefing(store)
        assert briefing["attention_needed"] is True
        assert briefing["active_alerts"] == 1
        assert briefing["sources"]["nas"]["status"] == "warning"
        assert "CPU at 96%" in briefing["sources"]["nas"]["headline"]
        assert "suggested_mention" in briefing

    def test_critical_alert_status(self, store):
        store.upsert_status("nas", ["infra"], {"metrics": {}, "ttl_sec": 3600})
        store.upsert_alert(
            "nas",
            ["infra"],
            "cpu-1",
            {
                "alert_id": "cpu-1",
                "level": "critical",
                "alert_type": "threshold",
                "message": "CPU at 99%",
                "resolved": False,
            },
        )
        briefing = generate_briefing(store)
        assert briefing["sources"]["nas"]["status"] == "critical"

    def test_suppressed_alert_filtered(self, store):
        store.upsert_status("nas", ["infra"], {"metrics": {}, "ttl_sec": 3600})
        store.upsert_alert(
            "nas",
            ["infra"],
            "cpu-1",
            {
                "alert_id": "cpu-1",
                "level": "warning",
                "alert_type": "threshold",
                "metric": "cpu_pct",
                "message": "CPU high",
                "resolved": False,
            },
        )
        now = datetime.now(timezone.utc)
        expires = now + timedelta(hours=1)
        store.add(
            Entry(
                id=make_id(),
                type=EntryType.SUPPRESSION,
                source="nas",
                tags=[],
                created=now,
                updated=now,
                expires=expires,
                data={
                    "metric": "cpu_pct",
                    "suppress_level": "warning",
                    "escalation_override": True,
                },
            )
        )
        briefing = generate_briefing(store)
        assert briefing["attention_needed"] is False
        assert briefing["active_alerts"] == 0
        assert briefing["sources"]["nas"]["status"] == "ok"

    def test_escalated_alert_breaks_through_suppression(self, store):
        store.upsert_status("nas", ["infra"], {"metrics": {}, "ttl_sec": 3600})
        store.upsert_alert(
            "nas",
            ["infra"],
            "cpu-1",
            {
                "alert_id": "cpu-1",
                "level": "critical",
                "alert_type": "threshold",
                "metric": "cpu_pct",
                "message": "CPU critical",
                "resolved": False,
            },
        )
        now = datetime.now(timezone.utc)
        expires = now + timedelta(hours=1)
        store.add(
            Entry(
                id=make_id(),
                type=EntryType.SUPPRESSION,
                source="nas",
                tags=[],
                created=now,
                updated=now,
                expires=expires,
                data={
                    "metric": "cpu_pct",
                    "suppress_level": "warning",
                    "escalation_override": True,
                },
            )
        )
        briefing = generate_briefing(store)
        assert briefing["attention_needed"] is True
        assert briefing["active_alerts"] == 1

    def test_pattern_filters_alert(self, store):
        store.upsert_status("nas", ["infra"], {"metrics": {}, "ttl_sec": 3600})
        store.upsert_alert(
            "nas",
            ["infra"],
            "qbt-stopped",
            {
                "alert_id": "qbt-stopped",
                "level": "warning",
                "alert_type": "structural",
                "message": "qBittorrent stopped",
                "resolved": False,
            },
        )
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
                data={
                    "description": "Maintenance on Fridays",
                    "conditions": {},
                    "effect": "suppress qbt-stopped",
                    "learned_from": "test",
                },
            )
        )
        briefing = generate_briefing(store)
        assert briefing["attention_needed"] is False
        assert briefing["active_alerts"] == 0

    def test_stale_source_detection(self, store):
        old = datetime.now(timezone.utc) - timedelta(seconds=300)
        store.upsert_status("nas", ["infra"], {"metrics": {}, "ttl_sec": 120})
        with store._conn.cursor() as cur:
            cur.execute(
                "UPDATE entries SET updated = %s, created = %s"
                " WHERE type = 'status' AND source = 'nas'",
                (old, old),
            )
        store._conn.commit()
        briefing = generate_briefing(store)
        assert briefing["attention_needed"] is True
        assert briefing["sources"]["nas"]["status"] == "stale"
        assert "not reported" in briefing["sources"]["nas"]["headline"]

    def test_resolved_alert_not_counted(self, store):
        store.upsert_status("nas", ["infra"], {"metrics": {}, "ttl_sec": 3600})
        store.upsert_alert(
            "nas",
            ["infra"],
            "cpu-1",
            {
                "alert_id": "cpu-1",
                "level": "warning",
                "alert_type": "threshold",
                "message": "CPU high",
                "resolved": True,
            },
        )
        briefing = generate_briefing(store)
        assert briefing["attention_needed"] is False
        assert briefing["active_alerts"] == 0

    def test_multiple_sources(self, store):
        store.upsert_status("nas", ["infra"], {"metrics": {}, "ttl_sec": 3600})
        store.upsert_status("ci", ["cicd"], {"metrics": {}, "ttl_sec": 3600})
        store.upsert_alert(
            "nas",
            ["infra"],
            "a1",
            {
                "alert_id": "a1",
                "level": "warning",
                "message": "NAS warn",
                "alert_type": "threshold",
                "resolved": False,
            },
        )
        store.upsert_alert(
            "ci",
            ["cicd"],
            "a2",
            {
                "alert_id": "a2",
                "level": "critical",
                "message": "CI critical",
                "alert_type": "threshold",
                "resolved": False,
            },
        )
        briefing = generate_briefing(store)
        assert briefing["active_alerts"] == 2
        assert briefing["sources"]["nas"]["status"] == "warning"
        assert briefing["sources"]["ci"]["status"] == "critical"
        assert "suggested_mention" in briefing

    def test_suppression_count(self, store):
        now = datetime.now(timezone.utc)
        expires = now + timedelta(hours=1)
        for i in range(3):
            store.add(
                Entry(
                    id=make_id(),
                    type=EntryType.SUPPRESSION,
                    source="nas",
                    tags=[],
                    created=now,
                    updated=now,
                    expires=expires,
                    data={"metric": f"m{i}", "suppress_level": "warning"},
                )
            )
        store.upsert_status("nas", ["infra"], {"metrics": {}, "ttl_sec": 3600})
        briefing = generate_briefing(store)
        assert briefing["active_suppressions"] == 3

    def test_drill_down_reference(self, store):
        store.upsert_status("nas", ["infra"], {"metrics": {}, "ttl_sec": 3600})
        store.upsert_alert(
            "nas",
            ["infra"],
            "a1",
            {
                "alert_id": "a1",
                "level": "warning",
                "message": "issue",
                "alert_type": "threshold",
                "resolved": False,
            },
        )
        briefing = generate_briefing(store)
        assert briefing["sources"]["nas"]["drill_down"] == "awareness://alerts/nas"

    # ------------------------------------------------------------------
    # Evaluation transparency
    # ------------------------------------------------------------------

    def test_evaluation_present_empty_store(self, store):
        """Evaluation trace is always present, even with no data."""
        briefing = generate_briefing(store)
        ev = briefing["evaluation"]
        assert ev["alerts_checked"] == 0
        assert ev["suppressed"] == 0
        assert ev["pattern_matched"] == 0
        assert ev["stale_sources"] == 0
        assert ev["surfaced"] == 0

    def test_evaluation_counts_alerts(self, store):
        """Evaluation counts all alerts checked and surfaced."""
        store.upsert_status("nas", ["infra"], {"metrics": {}, "ttl_sec": 3600})
        store.upsert_alert(
            "nas",
            ["infra"],
            "a1",
            {"alert_id": "a1", "level": "warning", "message": "test", "resolved": False},
        )
        store.upsert_alert(
            "nas",
            ["infra"],
            "a2",
            {"alert_id": "a2", "level": "critical", "message": "test2", "resolved": False},
        )
        briefing = generate_briefing(store)
        ev = briefing["evaluation"]
        assert ev["alerts_checked"] == 2
        assert ev["surfaced"] == 2
        assert ev["suppressed"] == 0
        assert ev["pattern_matched"] == 0

    def test_evaluation_counts_suppressed(self, store):
        """Evaluation tracks how many alerts were suppressed."""
        from datetime import timedelta

        from mcp_awareness.schema import Entry, EntryType, make_id, now_utc

        store.upsert_status("nas", ["infra"], {"metrics": {}, "ttl_sec": 3600})
        store.upsert_alert(
            "nas",
            ["infra"],
            "a1",
            {"alert_id": "a1", "level": "warning", "message": "CPU high", "resolved": False},
        )
        # Add a suppression that matches
        now = now_utc()
        store.add(
            Entry(
                id=make_id(),
                type=EntryType.SUPPRESSION,
                source="nas",
                tags=["infra"],
                created=now,
                updated=now,
                expires=now + timedelta(hours=1),
                data={"suppress_level": "warning", "escalation_override": True, "reason": "test"},
            )
        )
        briefing = generate_briefing(store)
        ev = briefing["evaluation"]
        assert ev["alerts_checked"] == 1
        assert ev["suppressed"] == 1
        assert ev["surfaced"] == 0

    def test_evaluation_counts_pattern_matched(self, store):
        """Evaluation tracks alerts dismissed by pattern matching."""
        from mcp_awareness.schema import Entry, EntryType, make_id, now_utc

        store.upsert_status("nas", ["infra"], {"metrics": {}, "ttl_sec": 3600})
        store.upsert_alert(
            "nas",
            ["infra"],
            "cpu-spike",
            {
                "alert_id": "cpu-spike",
                "level": "warning",
                "alert_type": "threshold",
                "metric": "cpu_pct",
                "message": "CPU spike detected",
                "resolved": False,
            },
        )
        # Add a pattern that matches this alert
        now = now_utc()
        store.add(
            Entry(
                id=make_id(),
                type=EntryType.PATTERN,
                source="nas",
                tags=["infra"],
                created=now,
                updated=now,
                expires=None,
                data={
                    "description": "Backup causes CPU spikes",
                    "conditions": {},
                    "effect": "CPU spike during backup",
                    "learned_from": "test",
                },
            )
        )
        briefing = generate_briefing(store)
        ev = briefing["evaluation"]
        assert ev["alerts_checked"] == 1
        assert ev["pattern_matched"] == 1
        assert ev["surfaced"] == 0

    def test_evaluation_stale_source_alerts_not_counted(self, store):
        """Alerts from stale sources are not counted in alerts_checked."""
        from datetime import datetime, timedelta, timezone

        store.upsert_status("nas", ["infra"], {"metrics": {}, "ttl_sec": 120})
        store.upsert_alert(
            "nas",
            ["infra"],
            "a1",
            {"alert_id": "a1", "level": "warning", "message": "test", "resolved": False},
        )
        # Backdate to make source stale
        old = datetime.now(timezone.utc) - timedelta(seconds=300)
        with store._conn.cursor() as cur:
            cur.execute(
                "UPDATE entries SET updated = %s, created = %s"
                " WHERE type = 'status' AND source = 'nas'",
                (old, old),
            )
        store._conn.commit()
        briefing = generate_briefing(store)
        ev = briefing["evaluation"]
        assert ev["stale_sources"] == 1
        # Alerts from stale sources are skipped, not evaluated
        assert ev["alerts_checked"] == 0
        assert ev["surfaced"] == 0

    def test_evaluation_counts_stale(self, store):
        """Evaluation tracks stale sources."""
        from datetime import datetime, timedelta, timezone

        store.upsert_status("nas", ["infra"], {"metrics": {}, "ttl_sec": 120})
        # Backdate to make it stale
        old = datetime.now(timezone.utc) - timedelta(seconds=300)
        with store._conn.cursor() as cur:
            cur.execute(
                "UPDATE entries SET updated = %s, created = %s"
                " WHERE type = 'status' AND source = 'nas'",
                (old, old),
            )
        store._conn.commit()
        briefing = generate_briefing(store)
        ev = briefing["evaluation"]
        assert ev["stale_sources"] == 1
