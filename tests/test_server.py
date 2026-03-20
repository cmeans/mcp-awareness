"""Tests for the FastMCP server handlers (resources + tools)."""

from __future__ import annotations

import json

import pytest

from mcp_awareness import server as server_mod
from mcp_awareness.store import SQLiteStore


@pytest.fixture(autouse=True)
def _use_temp_store(tmp_path: object, monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace the module-level store with a fresh temp store for each test."""
    temp_store = SQLiteStore(f"{tmp_path}/test.db")
    monkeypatch.setattr(server_mod, "store", temp_store)


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _store() -> SQLiteStore:
    return server_mod.store  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Resource tests
# ---------------------------------------------------------------------------


class TestBriefingResource:
    @pytest.mark.anyio
    async def test_empty_briefing(self) -> None:
        result = await server_mod.briefing_resource()
        data = json.loads(result)
        assert data["attention_needed"] is False
        assert data["active_alerts"] == 0

    @pytest.mark.anyio
    async def test_briefing_with_alert(self) -> None:
        s = _store()
        s.upsert_status("nas", ["infra"], {"metrics": {}, "ttl_sec": 3600})
        s.upsert_alert(
            "nas",
            ["infra"],
            "a1",
            {
                "alert_id": "a1",
                "level": "warning",
                "alert_type": "threshold",
                "message": "CPU high",
                "resolved": False,
            },
        )
        result = await server_mod.briefing_resource()
        data = json.loads(result)
        assert data["attention_needed"] is True
        assert data["active_alerts"] == 1


class TestAlertsResource:
    @pytest.mark.anyio
    async def test_empty_alerts(self) -> None:
        result = await server_mod.alerts_resource()
        assert json.loads(result) == []

    @pytest.mark.anyio
    async def test_alerts_returned(self) -> None:
        s = _store()
        s.upsert_alert(
            "nas",
            ["infra"],
            "a1",
            {
                "alert_id": "a1",
                "level": "warning",
                "alert_type": "threshold",
                "message": "CPU high",
                "resolved": False,
            },
        )
        result = await server_mod.alerts_resource()
        alerts = json.loads(result)
        assert len(alerts) == 1
        assert alerts[0]["data"]["alert_id"] == "a1"

    @pytest.mark.anyio
    async def test_source_alerts(self) -> None:
        s = _store()
        s.upsert_alert(
            "nas",
            ["infra"],
            "a1",
            {
                "alert_id": "a1",
                "level": "warning",
                "alert_type": "threshold",
                "message": "NAS issue",
                "resolved": False,
            },
        )
        s.upsert_alert(
            "ci",
            ["cicd"],
            "a2",
            {
                "alert_id": "a2",
                "level": "warning",
                "alert_type": "threshold",
                "message": "CI issue",
                "resolved": False,
            },
        )
        result = await server_mod.source_alerts_resource("nas")
        alerts = json.loads(result)
        assert len(alerts) == 1
        assert alerts[0]["source"] == "nas"


class TestStatusResource:
    @pytest.mark.anyio
    async def test_status_found(self) -> None:
        s = _store()
        s.upsert_status("nas", ["infra"], {"metrics": {"cpu": 42}, "ttl_sec": 120})
        result = await server_mod.source_status_resource("nas")
        data = json.loads(result)
        assert data["source"] == "nas"
        assert data["data"]["metrics"]["cpu"] == 42

    @pytest.mark.anyio
    async def test_status_not_found(self) -> None:
        result = await server_mod.source_status_resource("nonexistent")
        data = json.loads(result)
        assert "error" in data


class TestKnowledgeResource:
    @pytest.mark.anyio
    async def test_empty_knowledge(self) -> None:
        result = await server_mod.knowledge_resource()
        assert json.loads(result) == []

    @pytest.mark.anyio
    async def test_knowledge_after_learn(self) -> None:
        await server_mod.learn_pattern(
            source="nas",
            tags=["infra"],
            description="test pattern",
            conditions=None,
            effect="suppress test",
        )
        result = await server_mod.knowledge_resource()
        entries = json.loads(result)
        assert len(entries) == 1
        assert entries[0]["data"]["description"] == "test pattern"


class TestSuppressionsResource:
    @pytest.mark.anyio
    async def test_empty_suppressions(self) -> None:
        result = await server_mod.suppressions_resource()
        assert json.loads(result) == []

    @pytest.mark.anyio
    async def test_suppressions_after_suppress(self) -> None:
        await server_mod.suppress_alert(
            source="nas",
            metric="cpu_pct",
            reason="test",
        )
        result = await server_mod.suppressions_resource()
        entries = json.loads(result)
        assert len(entries) == 1


# ---------------------------------------------------------------------------
# Tool tests
# ---------------------------------------------------------------------------


class TestReportStatusTool:
    @pytest.mark.anyio
    async def test_report_status(self) -> None:
        result = await server_mod.report_status(
            source="nas",
            tags=["infra"],
            metrics={"cpu": 50},
        )
        data = json.loads(result)
        assert data["status"] == "ok"
        assert data["source"] == "nas"

    @pytest.mark.anyio
    async def test_report_status_with_inventory(self) -> None:
        result = await server_mod.report_status(
            source="nas",
            tags=["infra"],
            metrics={"cpu": 50},
            inventory={"docker": {"running": 5}},
        )
        data = json.loads(result)
        assert data["status"] == "ok"
        # Verify inventory was stored
        status = _store().get_latest_status("nas")
        assert status is not None
        assert status.data["inventory"]["docker"]["running"] == 5


class TestReportAlertTool:
    @pytest.mark.anyio
    async def test_report_alert(self) -> None:
        result = await server_mod.report_alert(
            source="nas",
            tags=["infra"],
            alert_id="cpu-1",
            level="warning",
            alert_type="threshold",
            message="CPU high",
        )
        data = json.loads(result)
        assert data["status"] == "ok"
        assert data["action"] == "reported"
        assert data["alert_id"] == "cpu-1"

    @pytest.mark.anyio
    async def test_report_alert_with_details_and_diagnostics(self) -> None:
        result = await server_mod.report_alert(
            source="nas",
            tags=["infra"],
            alert_id="cpu-1",
            level="critical",
            alert_type="threshold",
            message="CPU critical",
            details={"threshold": 95},
            diagnostics={"top_process": "qbt"},
        )
        data = json.loads(result)
        assert data["status"] == "ok"
        alerts = _store().get_active_alerts("nas")
        assert len(alerts) == 1
        assert alerts[0].data["details"] == {"threshold": 95}
        assert alerts[0].data["diagnostics"] == {"top_process": "qbt"}

    @pytest.mark.anyio
    async def test_resolve_alert(self) -> None:
        await server_mod.report_alert(
            source="nas",
            tags=["infra"],
            alert_id="cpu-1",
            level="warning",
            alert_type="threshold",
            message="CPU high",
        )
        result = await server_mod.report_alert(
            source="nas",
            tags=["infra"],
            alert_id="cpu-1",
            level="warning",
            alert_type="threshold",
            message="CPU high",
            resolved=True,
        )
        data = json.loads(result)
        assert data["action"] == "resolved"
        assert _store().get_active_alerts("nas") == []


class TestLearnPatternTool:
    @pytest.mark.anyio
    async def test_learn_pattern(self) -> None:
        result = await server_mod.learn_pattern(
            source="nas",
            tags=["infra"],
            description="qBittorrent stops on Fridays",
            conditions={"day_of_week": "friday"},
            effect="suppress qbittorrent_stopped",
        )
        data = json.loads(result)
        assert data["status"] == "ok"
        assert data["description"] == "qBittorrent stops on Fridays"
        patterns = _store().get_patterns("nas")
        assert len(patterns) == 1
        assert patterns[0].data["conditions"] == {"day_of_week": "friday"}
        assert patterns[0].data["learned_from"] == "conversation"

    @pytest.mark.anyio
    async def test_learn_pattern_defaults(self) -> None:
        result = await server_mod.learn_pattern(
            source="nas",
            tags=[],
            description="minimal",
        )
        data = json.loads(result)
        assert data["status"] == "ok"
        patterns = _store().get_patterns("nas")
        assert patterns[0].data["conditions"] == {}
        assert patterns[0].data["effect"] == ""


class TestSuppressAlertTool:
    @pytest.mark.anyio
    async def test_suppress_alert(self) -> None:
        result = await server_mod.suppress_alert(
            source="nas",
            metric="cpu_pct",
            duration_minutes=30,
            reason="user request",
        )
        data = json.loads(result)
        assert data["status"] == "ok"
        assert "expires" in data
        assert _store().count_active_suppressions() == 1

    @pytest.mark.anyio
    async def test_suppress_alert_global(self) -> None:
        result = await server_mod.suppress_alert(reason="silence everything")
        data = json.loads(result)
        assert data["status"] == "ok"
        supps = _store().get_active_suppressions("any-source")
        assert len(supps) == 1  # global suppression matches any source


class TestAddContextTool:
    @pytest.mark.anyio
    async def test_add_context(self) -> None:
        result = await server_mod.add_context(
            source="nas",
            tags=["infra"],
            description="sdb replaced, RAID rebuilt",
            expires_days=30,
        )
        data = json.loads(result)
        assert data["status"] == "ok"
        assert "expires" in data
        knowledge = _store().get_knowledge(tags=["infra"])
        assert len(knowledge) == 1
        assert knowledge[0].data["description"] == "sdb replaced, RAID rebuilt"


class TestSetPreferenceTool:
    @pytest.mark.anyio
    async def test_set_preference(self) -> None:
        result = await server_mod.set_preference(
            key="alert_verbosity",
            value="one_sentence",
        )
        data = json.loads(result)
        assert data["status"] == "ok"
        assert data["key"] == "alert_verbosity"
        assert data["value"] == "one_sentence"
        assert data["scope"] == "global"

    @pytest.mark.anyio
    async def test_set_preference_upserts(self) -> None:
        await server_mod.set_preference(
            key="alert_verbosity",
            value="verbose",
            scope="global",
        )
        await server_mod.set_preference(
            key="alert_verbosity",
            value="one_sentence",
            scope="global",
        )
        from mcp_awareness.schema import EntryType

        entries = _store().get_entries(entry_type=EntryType.PREFERENCE)
        assert len(entries) == 1
        assert entries[0].data["value"] == "one_sentence"

    @pytest.mark.anyio
    async def test_set_preference_scoped(self) -> None:
        result = await server_mod.set_preference(
            key="check_frequency",
            value="first_turn_only",
            scope="nas",
        )
        data = json.loads(result)
        assert data["scope"] == "nas"


# ---------------------------------------------------------------------------
# Read tool tests (mirrors of resources for tools-only clients)
# ---------------------------------------------------------------------------


class TestGetBriefingTool:
    @pytest.mark.anyio
    async def test_get_briefing_empty(self) -> None:
        result = await server_mod.get_briefing()
        data = json.loads(result)
        assert data["attention_needed"] is False

    @pytest.mark.anyio
    async def test_get_briefing_with_alert(self) -> None:
        s = _store()
        s.upsert_status("nas", ["infra"], {"metrics": {}, "ttl_sec": 3600})
        s.upsert_alert(
            "nas",
            ["infra"],
            "a1",
            {
                "alert_id": "a1",
                "level": "warning",
                "alert_type": "threshold",
                "message": "CPU high",
                "resolved": False,
            },
        )
        result = await server_mod.get_briefing()
        data = json.loads(result)
        assert data["attention_needed"] is True
        assert "suggested_mention" in data


class TestGetAlertsTool:
    @pytest.mark.anyio
    async def test_get_alerts_empty(self) -> None:
        result = await server_mod.get_alerts()
        assert json.loads(result) == []

    @pytest.mark.anyio
    async def test_get_alerts_filtered(self) -> None:
        s = _store()
        s.upsert_alert(
            "nas",
            ["infra"],
            "a1",
            {
                "alert_id": "a1",
                "level": "warning",
                "alert_type": "threshold",
                "message": "NAS issue",
                "resolved": False,
            },
        )
        s.upsert_alert(
            "ci",
            ["cicd"],
            "a2",
            {
                "alert_id": "a2",
                "level": "warning",
                "alert_type": "threshold",
                "message": "CI issue",
                "resolved": False,
            },
        )
        all_result = await server_mod.get_alerts()
        assert len(json.loads(all_result)) == 2
        nas_result = await server_mod.get_alerts(source="nas")
        assert len(json.loads(nas_result)) == 1


class TestGetStatusTool:
    @pytest.mark.anyio
    async def test_get_status(self) -> None:
        _store().upsert_status("nas", ["infra"], {"metrics": {"cpu": 42}, "ttl_sec": 120})
        result = await server_mod.get_status(source="nas")
        data = json.loads(result)
        assert data["data"]["metrics"]["cpu"] == 42

    @pytest.mark.anyio
    async def test_get_status_not_found(self) -> None:
        result = await server_mod.get_status(source="nonexistent")
        assert "error" in json.loads(result)


class TestGetKnowledgeTool:
    @pytest.mark.anyio
    async def test_get_knowledge(self) -> None:
        await server_mod.learn_pattern(
            source="nas",
            tags=["infra"],
            description="test",
            effect="suppress test",
        )
        result = await server_mod.get_knowledge()
        entries = json.loads(result)
        assert len(entries) == 1


class TestGetSuppressionsTool:
    @pytest.mark.anyio
    async def test_get_suppressions(self) -> None:
        await server_mod.suppress_alert(source="nas", metric="cpu_pct", reason="test")
        result = await server_mod.get_suppressions()
        entries = json.loads(result)
        assert len(entries) == 1


class TestDeleteEntryTool:
    @pytest.mark.anyio
    async def test_delete_by_id(self) -> None:
        result = await server_mod.learn_pattern(
            source="nas", tags=["infra"], description="to delete"
        )
        entry_id = json.loads(result)["id"]
        delete_result = await server_mod.delete_entry(entry_id=entry_id)
        data = json.loads(delete_result)
        assert data["status"] == "ok"
        assert data["trashed"] == 1
        assert data["recoverable_days"] == 30
        # Not visible in normal queries
        assert len(_store().get_patterns()) == 0
        # But in trash
        assert len(_store().get_deleted()) == 1

    @pytest.mark.anyio
    async def test_delete_by_id_not_found(self) -> None:
        result = await server_mod.delete_entry(entry_id="nonexistent")
        data = json.loads(result)
        assert data["trashed"] == 0

    @pytest.mark.anyio
    async def test_dry_run_without_confirm(self) -> None:
        await server_mod.learn_pattern(source="nas", tags=[], description="p1")
        await server_mod.learn_pattern(source="nas", tags=[], description="p2")
        result = await server_mod.delete_entry(source="nas", entry_type="pattern")
        data = json.loads(result)
        assert data["status"] == "dry_run"
        assert data["would_trash"] == 2
        # Nothing actually trashed
        assert len(_store().get_patterns("nas")) == 2

    @pytest.mark.anyio
    async def test_delete_by_source_with_confirm(self) -> None:
        await server_mod.learn_pattern(source="nas", tags=[], description="p1")
        await server_mod.learn_pattern(source="nas", tags=[], description="p2")
        await server_mod.learn_pattern(source="ci", tags=[], description="p3")
        result = await server_mod.delete_entry(
            source="nas", entry_type="pattern", confirm=True
        )
        data = json.loads(result)
        assert data["status"] == "ok"
        assert data["trashed"] == 2
        assert len(_store().get_patterns("nas")) == 0
        assert len(_store().get_patterns("ci")) == 1

    @pytest.mark.anyio
    async def test_delete_requires_source_or_id(self) -> None:
        result = await server_mod.delete_entry()
        data = json.loads(result)
        assert data["status"] == "error"


class TestRestoreEntryTool:
    @pytest.mark.anyio
    async def test_restore(self) -> None:
        result = await server_mod.learn_pattern(
            source="nas", tags=["infra"], description="restorable"
        )
        entry_id = json.loads(result)["id"]
        await server_mod.delete_entry(entry_id=entry_id)
        assert len(_store().get_patterns()) == 0
        restore_result = await server_mod.restore_entry(entry_id=entry_id)
        data = json.loads(restore_result)
        assert data["status"] == "ok"
        assert data["restored"] is True
        assert len(_store().get_patterns()) == 1

    @pytest.mark.anyio
    async def test_restore_not_found(self) -> None:
        result = await server_mod.restore_entry(entry_id="nonexistent")
        data = json.loads(result)
        assert data["status"] == "not_found"
        assert data["restored"] is False


class TestGetDeletedTool:
    @pytest.mark.anyio
    async def test_get_deleted_empty(self) -> None:
        result = await server_mod.get_deleted()
        assert json.loads(result) == []

    @pytest.mark.anyio
    async def test_get_deleted_shows_trashed(self) -> None:
        result = await server_mod.learn_pattern(
            source="nas", tags=[], description="trashed"
        )
        entry_id = json.loads(result)["id"]
        await server_mod.delete_entry(entry_id=entry_id)
        trash = json.loads(await server_mod.get_deleted())
        assert len(trash) == 1
        assert trash[0]["id"] == entry_id
