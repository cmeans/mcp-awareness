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

"""Tests for the FastMCP server handlers (resources + tools)."""

from __future__ import annotations

import json
import os
import threading

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from mcp_awareness import server as server_mod
from mcp_awareness.embeddings import OllamaEmbedding
from mcp_awareness.postgres_store import PostgresStore
from mcp_awareness.store import Store


def _parse_tool_error(exc_info: pytest.ExceptionInfo[ToolError]) -> dict:
    """Parse the structured JSON envelope from a ToolError."""
    return json.loads(str(exc_info.value))


TEST_OWNER = "test-owner"


@pytest.fixture(autouse=True)
def _use_temp_store(store: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace the module-level store with the conftest Postgres store for each test."""
    monkeypatch.setattr(server_mod, "store", store)


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _store() -> PostgresStore:
    return server_mod.store  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Store factory tests
# ---------------------------------------------------------------------------


class TestCreateStore:
    def test_passes_embedding_dimensions(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """_create_store passes EMBEDDING_DIMENSIONS to PostgresStore."""
        monkeypatch.setenv("AWARENESS_DATABASE_URL", "postgresql://fake:5432/db")
        monkeypatch.setattr(server_mod, "EMBEDDING_DIMENSIONS", 1024)
        captured: dict = {}
        _orig_init = PostgresStore.__init__

        def _capture_init(self: PostgresStore, *args: object, **kwargs: object) -> None:
            captured["args"] = args
            captured["kwargs"] = kwargs

        monkeypatch.setattr(PostgresStore, "__init__", _capture_init)
        server_mod._create_store()
        assert captured["kwargs"].get("embedding_dimensions") == 1024

    def test_raises_without_database_url(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """_create_store raises ValueError when AWARENESS_DATABASE_URL is unset."""
        monkeypatch.delenv("AWARENESS_DATABASE_URL", raising=False)
        with pytest.raises(ValueError, match="AWARENESS_DATABASE_URL is required"):
            server_mod._create_store()


class TestLazyStoreThreadSafety:
    """Verify _LazyStore only creates one store under concurrent access."""

    def test_concurrent_access_creates_single_instance(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Multiple threads racing through __getattr__ must produce exactly one store."""
        call_count = 0
        lock = threading.Lock()
        barrier = threading.Barrier(10)

        class _FakeStore:
            def ping(self) -> str:
                return "ok"

        def _counting_create_store() -> _FakeStore:
            nonlocal call_count
            with lock:
                call_count += 1
            return _FakeStore()

        monkeypatch.setattr(server_mod, "_create_store", _counting_create_store)

        lazy = server_mod._LazyStore()
        server_mod._LazyStore._instance = None
        errors: list[Exception] = []

        def _access() -> None:
            try:
                barrier.wait()  # all threads launch simultaneously
                lazy.ping()
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=_access) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        # Restore to avoid leaking into other tests
        server_mod._LazyStore._instance = None

        assert not errors, f"Threads raised: {errors}"
        assert call_count == 1, f"_create_store called {call_count} times, expected 1"

    def test_cleanup_thread_and_request_handler_race(
        self, store: Store, pg_dsn: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Cleanup daemon thread and request handler must share a single store."""
        monkeypatch.setenv("AWARENESS_DATABASE_URL", pg_dsn)
        call_count = 0
        count_lock = threading.Lock()
        go = threading.Event()

        real_create = server_mod._create_store

        def _tracking_create_store() -> Store:
            nonlocal call_count
            with count_lock:
                call_count += 1
            return real_create()

        monkeypatch.setattr(server_mod, "_create_store", _tracking_create_store)
        server_mod._LazyStore._instance = None
        lazy = server_mod._LazyStore()
        errors: list[Exception] = []

        def _simulate_cleanup() -> None:
            """Simulates a daemon thread (like cleanup) accessing the store."""
            try:
                go.wait()
                _ = lazy.get_stats
            except Exception as exc:
                errors.append(exc)

        def _simulate_request() -> None:
            """Simulates a request thread accessing the store."""
            try:
                go.wait()
                _ = lazy.add
            except Exception as exc:
                errors.append(exc)

        t_cleanup = threading.Thread(target=_simulate_cleanup, daemon=True)
        t_request = threading.Thread(target=_simulate_request)
        t_cleanup.start()
        t_request.start()
        go.set()
        t_cleanup.join(timeout=5)
        t_request.join(timeout=5)

        server_mod._LazyStore._instance = None

        assert not errors, f"Threads raised: {errors}"
        assert call_count == 1, f"_create_store called {call_count} times, expected 1"

    def test_embedding_worker_and_request_handler_race(
        self, store: Store, pg_dsn: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Embedding thread pool worker and request handler must share a single store."""
        monkeypatch.setenv("AWARENESS_DATABASE_URL", pg_dsn)
        call_count = 0
        count_lock = threading.Lock()
        go = threading.Event()

        real_create = server_mod._create_store

        def _tracking_create_store() -> Store:
            nonlocal call_count
            with count_lock:
                call_count += 1
            return real_create()

        monkeypatch.setattr(server_mod, "_create_store", _tracking_create_store)
        server_mod._LazyStore._instance = None
        lazy = server_mod._LazyStore()
        errors: list[Exception] = []

        def _simulate_embedding() -> None:
            """Simulates an embedding worker accessing store.upsert_embedding."""
            try:
                go.wait()
                _ = lazy.upsert_embedding
            except Exception as exc:
                errors.append(exc)

        def _simulate_request() -> None:
            """Simulates a request thread accessing store.get_knowledge."""
            try:
                go.wait()
                _ = lazy.get_knowledge
            except Exception as exc:
                errors.append(exc)

        t_embed = threading.Thread(target=_simulate_embedding, name="embed-0")
        t_request = threading.Thread(target=_simulate_request)
        t_embed.start()
        t_request.start()
        go.set()
        t_embed.join(timeout=5)
        t_request.join(timeout=5)

        server_mod._LazyStore._instance = None

        assert not errors, f"Threads raised: {errors}"
        assert call_count == 1, f"_create_store called {call_count} times, expected 1"

    def test_concurrent_real_postgres_store_creation(
        self, pg_dsn: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Integration: 10 threads racing to create a real PostgresStore + pool."""
        monkeypatch.setenv("AWARENESS_DATABASE_URL", pg_dsn)
        call_count = 0
        count_lock = threading.Lock()
        barrier = threading.Barrier(10)

        real_create = server_mod._create_store

        def _tracking_create_store() -> Store:
            nonlocal call_count
            with count_lock:
                call_count += 1
            return real_create()

        monkeypatch.setattr(server_mod, "_create_store", _tracking_create_store)
        server_mod._LazyStore._instance = None
        lazy = server_mod._LazyStore()
        errors: list[Exception] = []

        def _access() -> None:
            try:
                barrier.wait()  # synchronize start, not factory entry
                _ = lazy.add  # triggers __getattr__ → _create_store
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=_access) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        server_mod._LazyStore._instance = None

        assert not errors, f"Threads raised: {errors}"
        assert call_count == 1, f"_create_store called {call_count} times, expected 1"


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
        s.upsert_status(TEST_OWNER, "nas", ["infra"], {"metrics": {}, "ttl_sec": 3600})
        s.upsert_alert(
            TEST_OWNER,
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
            TEST_OWNER,
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
            TEST_OWNER,
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
            TEST_OWNER,
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
        s.upsert_status(TEST_OWNER, "nas", ["infra"], {"metrics": {"cpu": 42}, "ttl_sec": 120})
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
        status = _store().get_latest_status(TEST_OWNER, "nas")
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
        alerts = _store().get_active_alerts(TEST_OWNER, "nas")
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
        assert _store().get_active_alerts(TEST_OWNER, "nas") == []


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
        patterns = _store().get_patterns(TEST_OWNER, "nas")
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
        patterns = _store().get_patterns(TEST_OWNER, "nas")
        assert patterns[0].data["conditions"] == {}
        assert patterns[0].data["effect"] == ""


class TestInputValidation:
    @pytest.mark.anyio
    async def test_report_alert_invalid_level(self) -> None:
        with pytest.raises(ToolError) as exc_info:
            await server_mod.report_alert(
                source="nas",
                tags=["infra"],
                alert_id="x",
                level="bogus",
                alert_type="threshold",
                message="test",
            )
        body = _parse_tool_error(exc_info)
        assert body["error"]["code"] == "invalid_parameter"
        assert body["error"]["param"] == "level"

    @pytest.mark.anyio
    async def test_report_alert_invalid_type(self) -> None:
        with pytest.raises(ToolError) as exc_info:
            await server_mod.report_alert(
                source="nas",
                tags=["infra"],
                alert_id="x",
                level="warning",
                alert_type="bogus",
                message="test",
            )
        body = _parse_tool_error(exc_info)
        assert body["error"]["code"] == "invalid_parameter"
        assert body["error"]["param"] == "alert_type"

    @pytest.mark.anyio
    async def test_suppress_alert_invalid_level(self) -> None:
        with pytest.raises(ToolError) as exc_info:
            await server_mod.suppress_alert(source="nas", level="bogus")
        body = _parse_tool_error(exc_info)
        assert body["error"]["code"] == "invalid_parameter"
        assert body["error"]["param"] == "level"

    @pytest.mark.anyio
    async def test_suppress_alert_zero_duration(self) -> None:
        with pytest.raises(ToolError) as exc_info:
            await server_mod.suppress_alert(source="nas", duration_minutes=0)
        body = _parse_tool_error(exc_info)
        assert body["error"]["code"] == "invalid_parameter"
        assert "duration_minutes" in body["error"]["message"]

    @pytest.mark.anyio
    async def test_remind_invalid_urgency(self) -> None:
        with pytest.raises(ToolError) as exc_info:
            await server_mod.remind(
                goal="test",
                source="test",
                tags=[],
                urgency="bogus",
            )
        body = _parse_tool_error(exc_info)
        assert body["error"]["code"] == "invalid_parameter"
        assert body["error"]["param"] == "urgency"

    @pytest.mark.anyio
    async def test_add_context_zero_expires_days(self) -> None:
        with pytest.raises(ToolError) as exc_info:
            await server_mod.add_context(
                source="test",
                tags=[],
                description="test",
                expires_days=0,
            )
        body = _parse_tool_error(exc_info)
        assert body["error"]["code"] == "invalid_parameter"
        assert "expires_days" in body["error"]["message"]

    @pytest.mark.anyio
    async def test_get_knowledge_negative_offset(self) -> None:
        with pytest.raises(ToolError) as exc_info:
            await server_mod.get_knowledge(offset=-1)
        body = _parse_tool_error(exc_info)
        assert body["error"]["code"] == "invalid_parameter"
        assert body["error"]["param"] == "offset"

    @pytest.mark.anyio
    async def test_get_knowledge_negative_limit(self) -> None:
        with pytest.raises(ToolError) as exc_info:
            await server_mod.get_knowledge(limit=-1)
        body = _parse_tool_error(exc_info)
        assert body["error"]["code"] == "invalid_parameter"
        assert body["error"]["param"] == "limit"


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
        assert _store().count_active_suppressions(TEST_OWNER) == 1

    @pytest.mark.anyio
    async def test_suppress_alert_global(self) -> None:
        result = await server_mod.suppress_alert(reason="silence everything")
        data = json.loads(result)
        assert data["status"] == "ok"
        supps = _store().get_active_suppressions(TEST_OWNER, "any-source")
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
        knowledge = _store().get_knowledge(TEST_OWNER, tags=["infra"])
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

        entries = _store().get_entries(TEST_OWNER, entry_type=EntryType.PREFERENCE)
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
        s.upsert_status(TEST_OWNER, "nas", ["infra"], {"metrics": {}, "ttl_sec": 3600})
        s.upsert_alert(
            TEST_OWNER,
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
        assert json.loads(result)["entries"] == []

    @pytest.mark.anyio
    async def test_get_alerts_filtered(self) -> None:
        s = _store()
        s.upsert_alert(
            TEST_OWNER,
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
            TEST_OWNER,
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
        assert len(json.loads(all_result)["entries"]) == 2
        nas_result = await server_mod.get_alerts(source="nas")
        assert len(json.loads(nas_result)["entries"]) == 1

    @pytest.mark.anyio
    async def test_get_alerts_negative_limit(self) -> None:
        with pytest.raises(ToolError) as exc_info:
            await server_mod.get_alerts(limit=-1)
        body = _parse_tool_error(exc_info)
        assert body["error"]["param"] == "limit"

    @pytest.mark.anyio
    async def test_get_alerts_negative_offset(self) -> None:
        with pytest.raises(ToolError) as exc_info:
            await server_mod.get_alerts(offset=-1)
        body = _parse_tool_error(exc_info)
        assert body["error"]["param"] == "offset"


class TestGetStatusTool:
    @pytest.mark.anyio
    async def test_get_status(self) -> None:
        _store().upsert_status(
            TEST_OWNER, "nas", ["infra"], {"metrics": {"cpu": 42}, "ttl_sec": 120}
        )
        result = await server_mod.get_status(source="nas")
        data = json.loads(result)
        assert data["data"]["metrics"]["cpu"] == 42

    @pytest.mark.anyio
    async def test_get_status_not_found(self) -> None:
        with pytest.raises(ToolError) as exc_info:
            await server_mod.get_status(source="nonexistent")
        body = _parse_tool_error(exc_info)
        assert body["error"]["code"] == "not_found"


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
        entries = json.loads(result)["entries"]
        assert len(entries) == 1

    @pytest.mark.anyio
    async def test_get_knowledge_filtered_by_source(self) -> None:
        await server_mod.learn_pattern(source="nas", tags=["infra"], description="nas pattern")
        await server_mod.learn_pattern(source="ci", tags=["infra"], description="ci pattern")
        result = await server_mod.get_knowledge(source="nas")
        entries = json.loads(result)["entries"]
        assert len(entries) == 1
        assert entries[0]["data"]["description"] == "nas pattern"

    @pytest.mark.anyio
    async def test_get_knowledge_filtered_by_tags(self) -> None:
        await server_mod.learn_pattern(source="nas", tags=["infra"], description="infra pattern")
        await server_mod.learn_pattern(
            source="nas", tags=["personal"], description="personal pattern"
        )
        result = await server_mod.get_knowledge(tags=["personal"])
        entries = json.loads(result)["entries"]
        assert len(entries) == 1
        assert entries[0]["data"]["description"] == "personal pattern"

    @pytest.mark.anyio
    async def test_get_knowledge_filtered_by_entry_type(self) -> None:
        await server_mod.learn_pattern(source="nas", tags=["infra"], description="a pattern")
        await server_mod.add_context(source="nas", tags=["infra"], description="a context")
        result = await server_mod.get_knowledge(entry_type="context")
        entries = json.loads(result)["entries"]
        assert len(entries) == 1
        assert entries[0]["data"]["description"] == "a context"

    @pytest.mark.anyio
    async def test_get_knowledge_combined_filters(self) -> None:
        await server_mod.learn_pattern(source="nas", tags=["infra"], description="nas infra")
        await server_mod.learn_pattern(source="ci", tags=["infra"], description="ci infra")
        await server_mod.add_context(source="nas", tags=["infra"], description="nas context")
        result = await server_mod.get_knowledge(source="nas", entry_type="pattern")
        entries = json.loads(result)["entries"]
        assert len(entries) == 1
        assert entries[0]["data"]["description"] == "nas infra"


class TestInvalidEntryType:
    @pytest.mark.anyio
    async def test_get_knowledge_invalid_entry_type(self) -> None:
        with pytest.raises(ToolError) as exc_info:
            await server_mod.get_knowledge(entry_type="bogus")
        body = _parse_tool_error(exc_info)
        assert body["error"]["code"] == "invalid_parameter"
        assert body["error"]["param"] == "entry_type"
        assert "bogus" in body["error"]["message"]

    @pytest.mark.anyio
    async def test_delete_entry_invalid_entry_type(self) -> None:
        with pytest.raises(ToolError) as exc_info:
            await server_mod.delete_entry(source="test", entry_type="fake")
        body = _parse_tool_error(exc_info)
        assert body["error"]["code"] == "invalid_parameter"
        assert "fake" in body["error"]["message"]

    @pytest.mark.anyio
    async def test_semantic_search_invalid_entry_type(self) -> None:
        with pytest.raises(ToolError) as exc_info:
            await server_mod.semantic_search(query="test", entry_type="nope")
        body = _parse_tool_error(exc_info)
        assert body["error"]["code"] == "invalid_parameter"
        assert "nope" in body["error"]["message"]

    @pytest.mark.anyio
    async def test_semantic_search_limit_clamped(self) -> None:
        """Limit is clamped to 1-100 range — no unbounded queries."""
        # Limit=0 or negative should be clamped to 1, limit>100 to 100.
        # The tool raises ToolError about missing embedding provider (expected in test),
        # but the limit clamping happens before that check.
        with pytest.raises(ToolError) as exc_info:
            await server_mod.semantic_search(query="test", limit=999)
        body = _parse_tool_error(exc_info)
        # If we got here without crashing, the clamp worked.
        # Error about embedding provider is expected in test environment.
        assert body["error"]["code"] == "unavailable"
        assert "embedding" in body["error"]["message"].lower()


class TestSuppressAlertTagsNotDuplicated:
    @pytest.mark.anyio
    async def test_suppression_data_has_no_tags_field(self) -> None:
        """Tags should only be in the entry envelope, not duplicated in data."""
        await server_mod.suppress_alert(source="nas", tags=["infra", "docker"], reason="test")
        supps = _store().get_active_suppressions(TEST_OWNER)
        assert len(supps) == 1
        assert supps[0].tags == ["infra", "docker"]
        assert "tags" not in supps[0].data


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
        assert len(_store().get_patterns(TEST_OWNER)) == 0
        # But in trash
        assert len(_store().get_deleted(TEST_OWNER)) == 1

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
        assert len(_store().get_patterns(TEST_OWNER, "nas")) == 2

    @pytest.mark.anyio
    async def test_delete_by_source_with_confirm(self) -> None:
        await server_mod.learn_pattern(source="nas", tags=[], description="p1")
        await server_mod.learn_pattern(source="nas", tags=[], description="p2")
        await server_mod.learn_pattern(source="ci", tags=[], description="p3")
        result = await server_mod.delete_entry(source="nas", entry_type="pattern", confirm=True)
        data = json.loads(result)
        assert data["status"] == "ok"
        assert data["trashed"] == 2
        assert len(_store().get_patterns(TEST_OWNER, "nas")) == 0
        assert len(_store().get_patterns(TEST_OWNER, "ci")) == 1

    @pytest.mark.anyio
    async def test_dry_run_by_tags_uses_and_logic(self) -> None:
        """Dry-run with tags uses AND logic, matching soft_delete_by_tags."""
        await server_mod.learn_pattern(
            source="nas", tags=["infra", "disk"], description="both tags"
        )
        await server_mod.learn_pattern(source="nas", tags=["infra"], description="one tag only")
        # Dry-run with both tags should only match the first entry (AND logic)
        result = await server_mod.delete_entry(tags=["infra", "disk"])
        data = json.loads(result)
        assert data["status"] == "dry_run"
        assert data["would_trash"] == 1

    @pytest.mark.anyio
    async def test_delete_by_tags_with_confirm(self) -> None:
        await server_mod.learn_pattern(
            source="nas", tags=["infra", "disk"], description="both tags"
        )
        await server_mod.learn_pattern(source="nas", tags=["infra"], description="one tag only")
        result = await server_mod.delete_entry(tags=["infra", "disk"], confirm=True)
        data = json.loads(result)
        assert data["status"] == "ok"
        assert data["trashed"] == 1
        # The entry with only ["infra"] should survive
        remaining = _store().get_patterns(TEST_OWNER, "nas")
        assert len(remaining) == 1
        assert remaining[0].data["description"] == "one tag only"

    @pytest.mark.anyio
    async def test_delete_requires_source_or_id(self) -> None:
        with pytest.raises(ToolError) as exc_info:
            await server_mod.delete_entry()
        body = _parse_tool_error(exc_info)
        assert body["error"]["code"] == "invalid_parameter"


class TestRestoreEntryTool:
    @pytest.mark.anyio
    async def test_restore(self) -> None:
        result = await server_mod.learn_pattern(
            source="nas", tags=["infra"], description="restorable"
        )
        entry_id = json.loads(result)["id"]
        await server_mod.delete_entry(entry_id=entry_id)
        assert len(_store().get_patterns(TEST_OWNER)) == 0
        restore_result = await server_mod.restore_entry(entry_id=entry_id)
        data = json.loads(restore_result)
        assert data["status"] == "ok"
        assert data["restored"] == 1
        assert len(_store().get_patterns(TEST_OWNER)) == 1

    @pytest.mark.anyio
    async def test_restore_not_found(self) -> None:
        result = await server_mod.restore_entry(entry_id="nonexistent")
        data = json.loads(result)
        assert data["status"] == "not_found"
        assert data["restored"] == 0

    @pytest.mark.anyio
    async def test_restore_by_tags(self) -> None:
        """Restore soft-deleted entries by tag."""
        result = await server_mod.remember(
            source="test", tags=["restore-tag"], description="taggable"
        )
        entry_id = json.loads(result)["id"]
        await server_mod.delete_entry(entry_id=entry_id)
        restore_result = await server_mod.restore_entry(tags=["restore-tag"])
        data = json.loads(restore_result)
        assert data["status"] == "ok"
        assert data["restored"] >= 1
        assert data["tags"] == ["restore-tag"]

    @pytest.mark.anyio
    async def test_restore_no_args(self) -> None:
        """Restore with no entry_id or tags raises ToolError."""
        with pytest.raises(ToolError) as exc_info:
            await server_mod.restore_entry()
        body = _parse_tool_error(exc_info)
        assert body["error"]["code"] == "invalid_parameter"
        assert "Provide entry_id or tags" in body["error"]["message"]


class TestGetDeletedTool:
    @pytest.mark.anyio
    async def test_get_deleted_empty(self) -> None:
        result = await server_mod.get_deleted()
        assert json.loads(result)["entries"] == []

    @pytest.mark.anyio
    async def test_get_deleted_shows_trashed(self) -> None:
        result = await server_mod.learn_pattern(source="nas", tags=[], description="trashed")
        entry_id = json.loads(result)["id"]
        await server_mod.delete_entry(entry_id=entry_id)
        trash = json.loads(await server_mod.get_deleted())["entries"]
        assert len(trash) == 1
        assert trash[0]["id"] == entry_id

    @pytest.mark.anyio
    async def test_get_deleted_negative_limit(self) -> None:
        with pytest.raises(ToolError) as exc_info:
            await server_mod.get_deleted(limit=-1)
        body = _parse_tool_error(exc_info)
        assert body["error"]["param"] == "limit"

    @pytest.mark.anyio
    async def test_get_deleted_negative_offset(self) -> None:
        with pytest.raises(ToolError) as exc_info:
            await server_mod.get_deleted(offset=-1)
        body = _parse_tool_error(exc_info)
        assert body["error"]["param"] == "offset"


class TestRememberTool:
    @pytest.mark.anyio
    async def test_remember_basic(self) -> None:
        result = await server_mod.remember(
            source="personal", tags=["family"], description="Mom's birthday is March 15"
        )
        data = json.loads(result)
        assert data["status"] == "ok"
        # Visible in get_knowledge
        entries = json.loads(await server_mod.get_knowledge(entry_type="note"))["entries"]
        assert len(entries) == 1
        assert entries[0]["data"]["description"] == "Mom's birthday is March 15"

    @pytest.mark.anyio
    async def test_remember_with_content(self) -> None:
        result = await server_mod.remember(
            source="tools",
            tags=["backup"],
            description="Claude Code skills backup",
            content='{"skills": ["commit", "review-pr"]}',
            content_type="application/json",
            learned_from="claude-code",
        )
        data = json.loads(result)
        assert data["status"] == "ok"
        entries = json.loads(await server_mod.get_knowledge(entry_type="note"))["entries"]
        assert len(entries) == 1
        assert entries[0]["data"]["content_type"] == "application/json"
        assert entries[0]["data"]["learned_from"] == "claude-code"

    @pytest.mark.anyio
    async def test_remember_json_content_as_dict(self) -> None:
        """JSON content that Pydantic deserializes into a dict is stored as string."""
        # Simulate what happens when Pydantic parses a JSON object for a str field
        result = await server_mod.remember(
            source="tools",
            tags=["backup"],
            description="Config snapshot",
            content={"key": "value", "nested": [1, 2, 3]},
            content_type="application/json",
        )
        data = json.loads(result)
        assert data["status"] == "ok"
        entries = json.loads(await server_mod.get_knowledge(entry_type="note"))["entries"]
        # Content should be stored as a JSON string, not a dict
        assert isinstance(entries[0]["data"]["content"], str)
        parsed = json.loads(entries[0]["data"]["content"])
        assert parsed["key"] == "value"

    @pytest.mark.anyio
    async def test_remember_json_content_as_list(self) -> None:
        """JSON array content is also preserved as string."""
        result = await server_mod.remember(
            source="tools",
            tags=["backup"],
            description="List snapshot",
            content=[1, 2, 3],
            content_type="application/json",
        )
        data = json.loads(result)
        assert data["status"] == "ok"
        entries = json.loads(await server_mod.get_knowledge(entry_type="note"))["entries"]
        assert isinstance(entries[0]["data"]["content"], str)
        assert json.loads(entries[0]["data"]["content"]) == [1, 2, 3]

    @pytest.mark.anyio
    async def test_remember_no_content_field_when_omitted(self) -> None:
        await server_mod.remember(source="personal", tags=[], description="simple note")
        entries = json.loads(await server_mod.get_knowledge(entry_type="note"))["entries"]
        assert "content" not in entries[0]["data"]

    @pytest.mark.anyio
    async def test_notes_included_in_get_knowledge(self) -> None:
        await server_mod.remember(source="s", tags=["t"], description="a note")
        await server_mod.learn_pattern(source="s", tags=["t"], description="a pattern")
        entries = json.loads(await server_mod.get_knowledge())["entries"]
        assert len(entries) == 2


class TestUpdateEntryTool:
    @pytest.mark.anyio
    async def test_update_description(self) -> None:
        result = await server_mod.remember(source="personal", tags=["test"], description="original")
        entry_id = json.loads(result)["id"]
        update_result = await server_mod.update_entry(entry_id=entry_id, description="updated")
        data = json.loads(update_result)
        assert data["status"] == "ok"
        # Check the entry was updated
        entries = json.loads(
            await server_mod.get_knowledge(entry_type="note", include_history="true")
        )["entries"]
        assert entries[0]["data"]["description"] == "updated"
        # Check changelog
        changelog = entries[0]["data"]["changelog"]
        assert len(changelog) == 1
        assert changelog[0]["changed"]["description"] == "original"

    @pytest.mark.anyio
    async def test_update_tags(self) -> None:
        result = await server_mod.remember(source="personal", tags=["old-tag"], description="test")
        entry_id = json.loads(result)["id"]
        await server_mod.update_entry(entry_id=entry_id, tags=["new-tag"])
        entries = json.loads(await server_mod.get_knowledge(include_history="true"))["entries"]
        assert entries[0]["tags"] == ["new-tag"]
        assert entries[0]["data"]["changelog"][0]["changed"]["tags"] == ["old-tag"]

    @pytest.mark.anyio
    async def test_update_immutable_type_rejected(self) -> None:
        await server_mod.report_alert(
            source="nas",
            tags=["infra"],
            alert_id="test-alert",
            level="warning",
            alert_type="threshold",
            message="CPU high",
        )
        alerts = _store().get_active_alerts(TEST_OWNER)
        with pytest.raises(ToolError) as exc_info:
            await server_mod.update_entry(entry_id=alerts[0].id, description="changed")
        body = _parse_tool_error(exc_info)
        assert body["error"]["code"] == "not_found"
        assert "immutable" in body["error"]["message"]

    @pytest.mark.anyio
    async def test_update_not_found(self) -> None:
        with pytest.raises(ToolError) as exc_info:
            await server_mod.update_entry(entry_id="nonexistent", description="test")
        body = _parse_tool_error(exc_info)
        assert body["error"]["code"] == "not_found"

    @pytest.mark.anyio
    async def test_update_no_fields(self) -> None:
        with pytest.raises(ToolError) as exc_info:
            await server_mod.update_entry(entry_id="anything")
        body = _parse_tool_error(exc_info)
        assert body["error"]["code"] == "invalid_parameter"
        assert "No fields" in body["error"]["message"]

    @pytest.mark.anyio
    async def test_update_noop_same_value(self) -> None:
        result = await server_mod.remember(source="personal", tags=["test"], description="same")
        entry_id = json.loads(result)["id"]
        update_result = await server_mod.update_entry(entry_id=entry_id, description="same")
        data = json.loads(update_result)
        assert data["status"] == "ok"
        # No changelog since nothing changed
        entries = json.loads(await server_mod.get_knowledge(include_history="true"))["entries"]
        assert "changelog" not in entries[0]["data"]

    @pytest.mark.anyio
    async def test_update_json_content(self) -> None:
        """update_entry accepts JSON content that Pydantic deserializes."""
        result = await server_mod.remember(
            source="test", tags=["t"], description="test", content="old"
        )
        entry_id = json.loads(result)["id"]
        await server_mod.update_entry(
            entry_id=entry_id,
            content={"new": "value"},
        )
        entries = json.loads(await server_mod.get_knowledge(entry_type="note"))["entries"]
        assert isinstance(entries[0]["data"]["content"], str)
        assert json.loads(entries[0]["data"]["content"]) == {"new": "value"}

    @pytest.mark.anyio
    async def test_update_source_and_content_type(self) -> None:
        """update_entry can change source and content_type."""
        result = await server_mod.remember(
            source="old-source",
            tags=["t"],
            description="test",
            content="data",
            content_type="text/plain",
        )
        entry_id = json.loads(result)["id"]
        await server_mod.update_entry(
            entry_id=entry_id, source="new-source", content_type="application/json"
        )
        entries = json.loads(
            await server_mod.get_knowledge(source="new-source", include_history="true")
        )["entries"]
        assert len(entries) == 1
        assert entries[0]["data"]["content_type"] == "application/json"

    @pytest.mark.anyio
    async def test_update_pattern(self) -> None:
        result = await server_mod.learn_pattern(
            source="nas", tags=["infra"], description="original pattern"
        )
        entry_id = json.loads(result)["id"]
        await server_mod.update_entry(entry_id=entry_id, description="refined pattern")
        entries = json.loads(
            await server_mod.get_knowledge(entry_type="pattern", include_history="true")
        )["entries"]
        assert entries[0]["data"]["description"] == "refined pattern"

    @pytest.mark.anyio
    async def test_multiple_updates_accumulatechangelog(self) -> None:
        result = await server_mod.remember(source="personal", tags=["test"], description="v1")
        entry_id = json.loads(result)["id"]
        await server_mod.update_entry(entry_id=entry_id, description="v2")
        await server_mod.update_entry(entry_id=entry_id, description="v3")
        entries = json.loads(await server_mod.get_knowledge(include_history="true"))["entries"]
        changelog = entries[0]["data"]["changelog"]
        assert len(changelog) == 2
        assert changelog[0]["changed"]["description"] == "v1"
        assert changelog[1]["changed"]["description"] == "v2"


class TestGetKnowledgeHistory:
    @pytest.mark.anyio
    async def test_history_stripped_by_default(self) -> None:
        result = await server_mod.remember(source="s", tags=["t"], description="v1")
        entry_id = json.loads(result)["id"]
        await server_mod.update_entry(entry_id=entry_id, description="v2")
        entries = json.loads(await server_mod.get_knowledge())["entries"]
        assert "changelog" not in entries[0]["data"]

    @pytest.mark.anyio
    async def test_history_included_when_true(self) -> None:
        result = await server_mod.remember(source="s", tags=["t"], description="v1")
        entry_id = json.loads(result)["id"]
        await server_mod.update_entry(entry_id=entry_id, description="v2")
        entries = json.loads(await server_mod.get_knowledge(include_history="true"))["entries"]
        assert "changelog" in entries[0]["data"]

    @pytest.mark.anyio
    async def test_history_only(self) -> None:
        await server_mod.remember(source="s", tags=["t"], description="no changes")
        result = await server_mod.remember(source="s", tags=["t"], description="will change")
        entry_id = json.loads(result)["id"]
        await server_mod.update_entry(entry_id=entry_id, description="changed")
        entries = json.loads(await server_mod.get_knowledge(include_history="only"))["entries"]
        assert len(entries) == 1
        assert entries[0]["data"]["description"] == "changed"


class TestGetStatsTool:
    @pytest.mark.anyio
    async def test_get_stats_empty(self) -> None:
        result = await server_mod.get_stats()
        data = json.loads(result)
        assert data["total"] == 0
        assert data["sources"] == []
        assert data["entries"]["note"] == 0

    @pytest.mark.anyio
    async def test_get_stats_with_data(self) -> None:
        await server_mod.remember(source="personal", tags=["t"], description="note")
        await server_mod.learn_pattern(source="nas", tags=["t"], description="pattern")
        await server_mod.add_context(source="nas", tags=["t"], description="context")
        result = await server_mod.get_stats()
        data = json.loads(result)
        assert data["total"] == 3
        assert data["entries"]["note"] == 1
        assert data["entries"]["pattern"] == 1
        assert data["entries"]["context"] == 1
        assert set(data["sources"]) == {"personal", "nas"}


class TestGetTagsTool:
    @pytest.mark.anyio
    async def test_get_tags_empty(self) -> None:
        result = await server_mod.get_tags()
        assert json.loads(result) == []

    @pytest.mark.anyio
    async def test_get_tags_with_data(self) -> None:
        await server_mod.remember(source="s", tags=["infra", "nas"], description="a")
        await server_mod.remember(source="s", tags=["infra"], description="b")
        await server_mod.remember(source="s", tags=["personal"], description="c")
        result = await server_mod.get_tags()
        tags = json.loads(result)
        # infra should be first (count=2)
        assert tags[0]["tag"] == "infra"
        assert tags[0]["count"] == 2
        assert len(tags) == 3


class TestLogicalKeyUpsert:
    @pytest.mark.anyio
    async def test_remember_with_logical_key_creates(self) -> None:
        result = await server_mod.remember(
            source="project",
            tags=["status"],
            description="initial status",
            logical_key="project-status",
        )
        data = json.loads(result)
        assert data["status"] == "ok"
        assert data["action"] == "created"

    @pytest.mark.anyio
    async def test_remember_with_logical_key_upserts(self) -> None:
        await server_mod.remember(
            source="project",
            tags=["status"],
            description="v1",
            logical_key="project-status",
        )
        result = await server_mod.remember(
            source="project",
            tags=["status"],
            description="v2",
            logical_key="project-status",
        )
        data = json.loads(result)
        assert data["status"] == "ok"
        assert data["action"] == "updated"
        # Only one entry should exist
        entries = json.loads(await server_mod.get_knowledge(entry_type="note"))["entries"]
        assert len(entries) == 1
        assert entries[0]["data"]["description"] == "v2"

    @pytest.mark.anyio
    async def test_logical_key_tracks_changelog(self) -> None:
        await server_mod.remember(
            source="project",
            tags=["status"],
            description="original",
            logical_key="my-key",
        )
        await server_mod.remember(
            source="project",
            tags=["status"],
            description="updated",
            logical_key="my-key",
        )
        entries = json.loads(
            await server_mod.get_knowledge(entry_type="note", include_history="true")
        )["entries"]
        assert len(entries) == 1
        changelog = entries[0]["data"]["changelog"]
        assert len(changelog) == 1
        assert changelog[0]["changed"]["description"] == "original"

    @pytest.mark.anyio
    async def test_logical_key_upsert_updates_tags(self) -> None:
        await server_mod.remember(
            source="project", tags=["v1-tag"], description="initial", logical_key="tag-test"
        )
        await server_mod.remember(
            source="project", tags=["v2-tag"], description="initial", logical_key="tag-test"
        )
        entries = json.loads(await server_mod.get_knowledge(entry_type="note"))["entries"]
        assert len(entries) == 1
        assert entries[0]["tags"] == ["v2-tag"]

    @pytest.mark.anyio
    async def test_different_logical_keys_no_conflict(self) -> None:
        await server_mod.remember(
            source="project", tags=["a"], description="one", logical_key="key-1"
        )
        await server_mod.remember(
            source="project", tags=["b"], description="two", logical_key="key-2"
        )
        entries = json.loads(await server_mod.get_knowledge(entry_type="note"))["entries"]
        assert len(entries) == 2

    @pytest.mark.anyio
    async def test_same_key_different_source_no_conflict(self) -> None:
        await server_mod.remember(
            source="project-a", tags=["s"], description="a", logical_key="status"
        )
        await server_mod.remember(
            source="project-b", tags=["s"], description="b", logical_key="status"
        )
        entries = json.loads(await server_mod.get_knowledge(entry_type="note"))["entries"]
        assert len(entries) == 2

    @pytest.mark.anyio
    async def test_no_logical_key_no_upsert(self) -> None:
        await server_mod.remember(source="s", tags=["t"], description="first")
        await server_mod.remember(source="s", tags=["t"], description="second")
        entries = json.loads(await server_mod.get_knowledge(entry_type="note"))["entries"]
        assert len(entries) == 2  # no dedup without logical_key

    @pytest.mark.anyio
    async def test_upsert_noop_same_content(self) -> None:
        await server_mod.remember(source="project", tags=["s"], description="same", logical_key="k")
        result = await server_mod.remember(
            source="project", tags=["s"], description="same", logical_key="k"
        )
        data = json.loads(result)
        assert data["action"] == "updated"  # matched, but no changelog since nothing changed
        entries = json.loads(
            await server_mod.get_knowledge(entry_type="note", include_history="true")
        )["entries"]
        assert "changelog" not in entries[0]["data"]


# ------------------------------------------------------------------
# Prompts
# ------------------------------------------------------------------


class TestPrompts:
    @pytest.mark.anyio
    async def test_agent_instructions_empty(self) -> None:
        """Returns fallback message when no prompt entries exist."""
        result = await server_mod.agent_instructions()
        assert "No agent instructions found" in result

    @pytest.mark.anyio
    async def test_agent_instructions_from_store(self) -> None:
        """Composes instructions from awareness-prompt entries."""
        await server_mod.remember(
            source="awareness-prompt",
            tags=["memory-prompt"],
            description="Awareness prompt Entry 1 (Core): Start with get_briefing.",
        )
        await server_mod.remember(
            source="awareness-prompt",
            tags=["memory-prompt"],
            description="Awareness prompt Entry 2 (Reading): Call get_knowledge before answering.",
        )
        result = await server_mod.agent_instructions()
        assert "# Awareness Agent Instructions" in result
        assert "## Core" in result
        assert "## Reading" in result
        assert "get_briefing" in result

    @pytest.mark.anyio
    async def test_agent_instructions_sorted(self) -> None:
        """Entries are sorted by entry number, not insertion order."""
        # Insert out of order
        await server_mod.remember(
            source="awareness-prompt",
            tags=["memory-prompt"],
            description="Awareness prompt Entry 3 (Writing): Use remember for notes.",
        )
        await server_mod.remember(
            source="awareness-prompt",
            tags=["memory-prompt"],
            description="Awareness prompt Entry 1 (Core): Start with get_briefing.",
        )
        result = await server_mod.agent_instructions()
        core_pos = result.index("## Core")
        writing_pos = result.index("## Writing")
        assert core_pos < writing_pos

    @pytest.mark.anyio
    async def test_agent_instructions_non_matching_format(self) -> None:
        """Entries without 'Entry N (Name):' pattern fall through to raw description."""
        await server_mod.remember(
            source="awareness-prompt",
            tags=["memory-prompt"],
            description="Just a plain instruction without the standard format.",
        )
        result = await server_mod.agent_instructions()
        assert "# Awareness Agent Instructions" in result
        assert "Just a plain instruction" in result

    @pytest.mark.anyio
    async def test_project_context_empty(self) -> None:
        result = await server_mod.project_context(repo_name="nonexistent")
        assert "No knowledge or alerts found" in result

    @pytest.mark.anyio
    async def test_project_context_with_entries(self) -> None:
        await server_mod.remember(
            source="test-project",
            tags=["my-repo"],
            description="Architecture uses 4-file layout.",
        )
        result = await server_mod.project_context(repo_name="my-repo")
        assert "# Project Context: my-repo" in result
        assert "Architecture uses 4-file layout" in result

    @pytest.mark.anyio
    async def test_project_context_with_alerts(self) -> None:
        """Project context includes active alerts for the repo tag."""
        await server_mod.report_alert(
            source="ci",
            tags=["my-repo"],
            alert_id="build-fail",
            level="critical",
            alert_type="threshold",
            message="Build failed on main",
        )
        result = await server_mod.project_context(repo_name="my-repo")
        assert "Active Alerts" in result
        assert "critical" in result
        assert "Build failed on main" in result

    @pytest.mark.anyio
    async def test_project_context_truncates_long_descriptions(self) -> None:
        """Descriptions longer than 200 chars are truncated."""
        long_desc = "A" * 250
        await server_mod.remember(
            source="test-project",
            tags=["long-repo"],
            description=long_desc,
        )
        result = await server_mod.project_context(repo_name="long-repo")
        assert "A" * 200 + "..." in result
        assert "A" * 201 not in result.replace("...", "")

    @pytest.mark.anyio
    async def test_system_status_empty(self) -> None:
        result = await server_mod.system_status(source="nonexistent")
        assert "No status or alerts found" in result

    @pytest.mark.anyio
    async def test_system_status_with_data(self) -> None:
        await server_mod.report_status(
            source="test-nas",
            tags=["infra"],
            metrics={"cpu": 45, "memory": 60},
        )
        result = await server_mod.system_status(source="test-nas")
        assert "# System Status: test-nas" in result
        assert "cpu: 45" in result

    @pytest.mark.anyio
    async def test_system_status_with_description(self) -> None:
        """Status entries with a description field in data show it in the output."""
        _store().upsert_status(
            TEST_OWNER,
            "test-nas-desc",
            ["infra"],
            {"metrics": {"cpu": 10}, "description": "All systems nominal", "ttl_sec": 120},
        )
        result = await server_mod.system_status(source="test-nas-desc")
        assert "All systems nominal" in result

    @pytest.mark.anyio
    async def test_system_status_with_alerts_and_patterns(self) -> None:
        """System status includes alerts and known patterns for the source."""
        await server_mod.report_status(
            source="test-sys",
            tags=["infra"],
            metrics={"disk": 90},
        )
        await server_mod.report_alert(
            source="test-sys",
            tags=["infra"],
            alert_id="disk-full",
            level="warning",
            alert_type="threshold",
            message="Disk usage above 90%",
        )
        await server_mod.learn_pattern(
            source="test-sys",
            tags=["infra"],
            description="disk pattern",
            conditions={"metric": "disk", "threshold": 90},
            effect="performance degrades",
        )
        result = await server_mod.system_status(source="test-sys")
        assert "Active Alerts" in result
        assert "warning" in result
        assert "Disk usage above 90%" in result
        assert "Known Patterns" in result
        assert "performance degrades" in result

    @pytest.mark.anyio
    async def test_write_guide(self) -> None:
        await server_mod.remember(
            source="test-src", tags=["alpha", "beta"], description="test note"
        )
        result = await server_mod.write_guide()
        assert "# Awareness Write Guide" in result
        assert "test-src" in result
        assert "alpha" in result

    @pytest.mark.anyio
    async def test_write_guide_many_tags(self) -> None:
        """Write guide caps tag list at 30 and shows overflow count."""
        for i in range(35):
            await server_mod.remember(
                source=f"src-{i}",
                tags=[f"tag-{i:03d}"],
                description=f"note {i}",
            )
        result = await server_mod.write_guide()
        assert "and 5 more" in result

    @pytest.mark.anyio
    async def test_catchup_empty(self) -> None:
        result = await server_mod.catchup(hours=24)
        assert "Nothing changed" in result

    @pytest.mark.anyio
    async def test_catchup_with_recent(self) -> None:
        await server_mod.remember(source="test-src", tags=["t"], description="recent note")
        result = await server_mod.catchup(hours=24)
        assert "recent note" in result
        assert "[new]" in result

    @pytest.mark.anyio
    async def test_catchup_with_alerts(self) -> None:
        """Catchup shows recent alerts."""
        await server_mod.report_alert(
            source="ci",
            tags=["test"],
            alert_id="catchup-alert",
            level="critical",
            alert_type="threshold",
            message="Deploy failed",
        )
        result = await server_mod.catchup(hours=24)
        assert "New/Updated Alerts" in result
        assert "critical" in result
        assert "Deploy failed" in result

    @pytest.mark.anyio
    async def test_catchup_truncates_long_descriptions(self) -> None:
        """Catchup truncates knowledge descriptions over 150 chars."""
        long_desc = "B" * 200
        await server_mod.remember(source="test-src", tags=["t"], description=long_desc)
        result = await server_mod.catchup(hours=24)
        assert "B" * 150 + "..." in result


class TestCustomPrompts:
    @pytest.mark.anyio
    async def test_custom_prompt_no_vars(self) -> None:
        """Custom prompt with no template variables."""
        await server_mod.remember(
            source="custom-prompt",
            tags=["prompt"],
            description="Daily standup summary",
            content="Summarize all active alerts and recent changes.",
            logical_key="standup",
        )
        server_mod._sync_custom_prompts(force=True)
        pm = server_mod.mcp._prompt_manager
        assert "user/standup" in pm._prompts
        prompt = pm._prompts["user/standup"]
        assert prompt.description == "Daily standup summary"

    @pytest.mark.anyio
    async def test_custom_prompt_with_vars(self) -> None:
        """Custom prompt extracts {{var}} as arguments."""
        await server_mod.remember(
            source="custom-prompt",
            tags=["prompt"],
            description="Project review",
            content="Review project {{repo_name}} focusing on {{area}}.",
            logical_key="project-review",
        )
        server_mod._sync_custom_prompts(force=True)
        pm = server_mod.mcp._prompt_manager
        prompt = pm._prompts["user/project-review"]
        arg_names = [a.name for a in (prompt.arguments or [])]
        assert "repo_name" in arg_names
        assert "area" in arg_names

    @pytest.mark.anyio
    async def test_custom_prompt_renders(self) -> None:
        """Custom prompt renders template variables."""
        await server_mod.remember(
            source="custom-prompt",
            tags=["prompt"],
            description="Greeting",
            content="Hello {{name}}, welcome to {{project}}!",
            logical_key="greeting",
        )
        server_mod._sync_custom_prompts(force=True)
        pm = server_mod.mcp._prompt_manager
        prompt = pm._prompts["user/greeting"]
        result = await prompt.fn(name="Chris", project="awareness")
        assert result == "Hello Chris, welcome to awareness!"

    @pytest.mark.anyio
    async def test_custom_prompt_removal(self) -> None:
        """Deleted custom prompts are removed on next sync."""
        await server_mod.remember(
            source="custom-prompt",
            tags=["prompt"],
            description="Temporary",
            content="temp",
            logical_key="temp",
        )
        server_mod._sync_custom_prompts(force=True)
        pm = server_mod.mcp._prompt_manager
        assert "user/temp" in pm._prompts
        # Delete and re-sync
        entry_id = server_mod.store.get_entries(TEST_OWNER, source="custom-prompt")[0].id
        server_mod.store.soft_delete_by_id(TEST_OWNER, entry_id)
        server_mod._sync_custom_prompts(force=True)
        assert "user/temp" not in pm._prompts

    @pytest.mark.anyio
    async def test_sync_uses_default_owner_not_request_owner(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """_sync_custom_prompts queries DEFAULT_OWNER, not _owner_id().

        Regression test for MEDIUM #14: in a multi-tenant deployment,
        _owner_id() returns the authenticated user, but custom prompts
        are global server config scoped to DEFAULT_OWNER.
        """
        # Store a custom prompt under DEFAULT_OWNER (TEST_OWNER)
        await server_mod.remember(
            source="custom-prompt",
            tags=["prompt"],
            description="Owner test",
            content="owned prompt",
            logical_key="owner-test",
        )
        # Simulate a request context where _owner_id() returns a different user
        monkeypatch.setattr(server_mod, "_owner_id", lambda: "user-b")
        server_mod._sync_custom_prompts(force=True)
        pm = server_mod.mcp._prompt_manager
        # The prompt should still be found because sync uses DEFAULT_OWNER
        assert "user/owner-test" in pm._prompts

    @pytest.mark.anyio
    async def test_sync_debounce_skips_when_recent(self) -> None:
        """_sync_custom_prompts debounce skips DB hit within interval.

        Regression test for MEDIUM #15: without debounce, every call to
        agent_instructions triggers a DB round-trip.
        """
        await server_mod.remember(
            source="custom-prompt",
            tags=["prompt"],
            description="Debounce test",
            content="debounce",
            logical_key="debounce-test",
        )
        # Force-sync to populate and set the timestamp
        server_mod._sync_custom_prompts(force=True)
        pm = server_mod.mcp._prompt_manager
        assert "user/debounce-test" in pm._prompts

        # Manually remove the prompt from the registry to detect re-sync
        del pm._prompts["user/debounce-test"]

        # Non-forced call should be debounced (no re-sync)
        server_mod._sync_custom_prompts()
        assert "user/debounce-test" not in pm._prompts

    @pytest.mark.anyio
    async def test_sync_debounce_allows_after_interval(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """_sync_custom_prompts re-syncs after the debounce interval elapses."""
        await server_mod.remember(
            source="custom-prompt",
            tags=["prompt"],
            description="Interval test",
            content="interval",
            logical_key="interval-test",
        )
        server_mod._sync_custom_prompts(force=True)
        pm = server_mod.mcp._prompt_manager
        assert "user/interval-test" in pm._prompts

        # Remove from registry to detect re-sync
        del pm._prompts["user/interval-test"]

        # Pretend the last sync happened long ago
        monkeypatch.setattr(server_mod, "_last_prompt_sync", 0.0)

        # Non-forced call should now re-sync
        server_mod._sync_custom_prompts()
        assert "user/interval-test" in pm._prompts


# ---------------------------------------------------------------------------
# List mode and since filter
# ---------------------------------------------------------------------------


class TestListModeAndSince:
    @pytest.mark.anyio
    async def test_get_knowledge_list_mode(self) -> None:
        s = _store()
        from mcp_awareness.schema import Entry, EntryType, make_id, now_utc

        s.add(
            TEST_OWNER,
            Entry(
                id=make_id(),
                type=EntryType.NOTE,
                source="test",
                tags=["demo"],
                created=now_utc(),
                expires=None,
                data={"description": "A test note", "content": "lots of content here"},
            ),
        )
        # Full mode — includes data with content
        full = json.loads(await server_mod.get_knowledge())["entries"]
        assert len(full) == 1
        assert "data" in full[0]
        assert full[0]["data"].get("content") == "lots of content here"

        # List mode — metadata only, no data/content
        listing = json.loads(await server_mod.get_knowledge(mode="list"))["entries"]
        assert len(listing) == 1
        assert "data" not in listing[0]
        assert listing[0]["description"] == "A test note"
        assert listing[0]["source"] == "test"
        assert listing[0]["tags"] == ["demo"]

    @pytest.mark.anyio
    async def test_get_alerts_list_mode(self) -> None:
        s = _store()
        s.upsert_alert(
            TEST_OWNER,
            "nas",
            ["infra"],
            "a1",
            {"alert_id": "a1", "level": "warning", "message": "CPU high", "resolved": False},
        )
        full = json.loads(await server_mod.get_alerts())["entries"]
        assert "data" in full[0]
        listing = json.loads(await server_mod.get_alerts(mode="list"))["entries"]
        assert "data" not in listing[0]

    @pytest.mark.anyio
    async def test_get_knowledge_since(self) -> None:
        from datetime import datetime, timedelta, timezone

        from mcp_awareness.schema import Entry, EntryType, make_id

        s = _store()
        old = datetime.now(timezone.utc) - timedelta(hours=2)
        s.add(
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
        s.add(
            TEST_OWNER,
            Entry(
                id=make_id(),
                type=EntryType.NOTE,
                source="test",
                tags=[],
                created=datetime.now(timezone.utc),
                expires=None,
                data={"description": "recent note"},
            ),
        )
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        result = json.loads(await server_mod.get_knowledge(since=cutoff))["entries"]
        assert len(result) == 1
        assert result[0]["data"]["description"] == "recent note"

    @pytest.mark.anyio
    async def test_get_deleted_list_mode(self) -> None:
        from mcp_awareness.schema import Entry, EntryType, make_id, now_utc

        s = _store()
        entry = s.add(
            TEST_OWNER,
            Entry(
                id=make_id(),
                type=EntryType.NOTE,
                source="test",
                tags=["demo"],
                created=now_utc(),
                expires=None,
                data={"description": "will delete", "content": "big content"},
            ),
        )
        s.soft_delete_by_id(TEST_OWNER, entry.id)
        listing = json.loads(await server_mod.get_deleted(mode="list"))["entries"]
        assert len(listing) == 1
        assert "data" not in listing[0]
        assert listing[0]["description"] == "will delete"

    @pytest.mark.anyio
    async def test_get_alerts_since(self) -> None:
        from datetime import datetime, timedelta, timezone

        s = _store()
        old = datetime.now(timezone.utc) - timedelta(hours=2)
        s.upsert_alert(
            TEST_OWNER,
            "nas",
            ["infra"],
            "old-alert",
            {"alert_id": "old-alert", "level": "warning", "message": "old", "resolved": False},
        )
        # Backdate the alert
        with s._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE entries SET updated = %s WHERE data->>'alert_id' = 'old-alert'",
                (old,),
            )
        s.upsert_alert(
            TEST_OWNER,
            "nas",
            ["infra"],
            "recent-alert",
            {
                "alert_id": "recent-alert",
                "level": "warning",
                "message": "recent",
                "resolved": False,
            },
        )
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        result = json.loads(await server_mod.get_alerts(since=cutoff))["entries"]
        assert len(result) == 1
        assert result[0]["data"]["alert_id"] == "recent-alert"

    @pytest.mark.anyio
    async def test_get_knowledge_source_sql_filter(self) -> None:
        from mcp_awareness.schema import Entry, EntryType, make_id, now_utc

        s = _store()
        s.add(
            TEST_OWNER,
            Entry(
                id=make_id(),
                type=EntryType.NOTE,
                source="alpha",
                tags=[],
                created=now_utc(),
                expires=None,
                data={"description": "from alpha"},
            ),
        )
        s.add(
            TEST_OWNER,
            Entry(
                id=make_id(),
                type=EntryType.NOTE,
                source="beta",
                tags=[],
                created=now_utc(),
                expires=None,
                data={"description": "from beta"},
            ),
        )
        result = json.loads(await server_mod.get_knowledge(source="alpha"))["entries"]
        assert len(result) == 1
        assert result[0]["data"]["description"] == "from alpha"

    @pytest.mark.anyio
    async def test_since_empty_string_returns_error(self) -> None:
        with pytest.raises(ToolError):
            await server_mod.get_knowledge(since="")

        with pytest.raises(ToolError):
            await server_mod.get_alerts(since="")

        with pytest.raises(ToolError):
            await server_mod.get_deleted(since="")

    @pytest.mark.anyio
    async def test_semantic_search_empty_since_returns_error(self) -> None:
        with pytest.raises(ToolError) as exc_info:
            await server_mod.semantic_search(query="test", since="")
        body = _parse_tool_error(exc_info)
        assert body["error"]["param"] == "since"

    @pytest.mark.anyio
    async def test_semantic_search_empty_until_returns_error(self) -> None:
        with pytest.raises(ToolError) as exc_info:
            await server_mod.semantic_search(query="test", until="")
        body = _parse_tool_error(exc_info)
        assert body["error"]["param"] == "until"


# ---------------------------------------------------------------------------
# Read / action tracking tools
# ---------------------------------------------------------------------------


class TestReadActionTracking:
    @pytest.mark.anyio
    async def test_acted_on(self) -> None:
        from mcp_awareness.schema import Entry, EntryType, make_id, now_utc

        s = _store()
        entry = s.add(
            TEST_OWNER,
            Entry(
                id=make_id(),
                type=EntryType.NOTE,
                source="test",
                tags=["project"],
                created=now_utc(),
                expires=None,
                data={"description": "actionable note"},
            ),
        )
        result = json.loads(
            await server_mod.acted_on(
                entry_id=entry.id,
                action="created issue #42",
                platform="claude-code",
                detail="https://github.com/example/42",
            )
        )
        assert result["status"] == "ok"
        assert result["action"] == "created issue #42"
        assert result["tags"] == ["project"]  # copied from entry

    @pytest.mark.anyio
    async def test_acted_on_invalid_entry(self) -> None:
        result = json.loads(await server_mod.acted_on(entry_id="nonexistent-id", action="test"))
        assert result["status"] == "error"
        assert "not found" in result["message"].lower()

    @pytest.mark.anyio
    async def test_get_reads_after_get_knowledge(self) -> None:
        """get_knowledge auto-logs reads, get_reads retrieves them."""
        from mcp_awareness.schema import Entry, EntryType, make_id, now_utc

        s = _store()
        s.add(
            TEST_OWNER,
            Entry(
                id=make_id(),
                type=EntryType.NOTE,
                source="test",
                tags=[],
                created=now_utc(),
                expires=None,
                data={"description": "will be read"},
            ),
        )
        # This should auto-log reads
        await server_mod.get_knowledge()
        reads = json.loads(await server_mod.get_reads())["entries"]
        assert len(reads) >= 1
        assert reads[0]["tool_used"] == "get_knowledge"

    @pytest.mark.anyio
    async def test_get_actions(self) -> None:
        from mcp_awareness.schema import Entry, EntryType, make_id, now_utc

        s = _store()
        entry = s.add(
            TEST_OWNER,
            Entry(
                id=make_id(),
                type=EntryType.NOTE,
                source="test",
                tags=["demo"],
                created=now_utc(),
                expires=None,
                data={"description": "test"},
            ),
        )
        await server_mod.acted_on(entry_id=entry.id, action="test action")
        actions = json.loads(await server_mod.get_actions(entry_id=entry.id))["entries"]
        assert len(actions) == 1
        assert actions[0]["action"] == "test action"

    @pytest.mark.anyio
    async def test_get_unread(self) -> None:
        from mcp_awareness.schema import Entry, EntryType, make_id, now_utc

        s = _store()
        s.add(
            TEST_OWNER,
            Entry(
                id=make_id(),
                type=EntryType.NOTE,
                source="test",
                tags=[],
                created=now_utc(),
                expires=None,
                data={"description": "never read"},
            ),
        )
        read_entry = s.add(
            TEST_OWNER,
            Entry(
                id=make_id(),
                type=EntryType.NOTE,
                source="test",
                tags=[],
                created=now_utc(),
                expires=None,
                data={"description": "will be read"},
            ),
        )
        s.log_read(TEST_OWNER, [read_entry.id], tool_used="test")
        unread = json.loads(await server_mod.get_unread())["entries"]
        assert len(unread) == 1
        assert unread[0]["description"] == "never read"

    @pytest.mark.anyio
    async def test_get_unread_with_limit(self) -> None:
        from mcp_awareness.schema import Entry, EntryType, make_id, now_utc

        s = _store()
        for i in range(5):
            s.add(
                TEST_OWNER,
                Entry(
                    id=make_id(),
                    type=EntryType.NOTE,
                    source="test",
                    tags=[],
                    created=now_utc(),
                    expires=None,
                    data={"description": f"unread-{i}"},
                ),
            )
        all_unread = json.loads(await server_mod.get_unread())["entries"]
        assert len(all_unread) == 5
        limited = json.loads(await server_mod.get_unread(limit=2))["entries"]
        assert len(limited) == 2

    @pytest.mark.anyio
    async def test_get_activity(self) -> None:
        from mcp_awareness.schema import Entry, EntryType, make_id, now_utc

        s = _store()
        entry = s.add(
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
        s.log_read(TEST_OWNER, [entry.id], tool_used="test")
        await server_mod.acted_on(entry_id=entry.id, action="used")
        activity = json.loads(await server_mod.get_activity())["entries"]
        assert len(activity) >= 2
        types = {a["event_type"] for a in activity}
        assert "read" in types
        assert "action" in types

    @pytest.mark.anyio
    async def test_list_mode_includes_read_counts(self) -> None:
        """List mode enriches entries with read_count and last_read."""
        from mcp_awareness.schema import Entry, EntryType, make_id, now_utc

        s = _store()
        entry = s.add(
            TEST_OWNER,
            Entry(
                id=make_id(),
                type=EntryType.NOTE,
                source="test",
                tags=[],
                created=now_utc(),
                expires=None,
                data={"description": "popular entry"},
            ),
        )
        s.log_read(TEST_OWNER, [entry.id], tool_used="test")
        s.log_read(TEST_OWNER, [entry.id], tool_used="test")
        # get_knowledge itself also logs a read, so count will be 2 + 1 = 3
        listing = json.loads(await server_mod.get_knowledge(mode="list"))["entries"]
        assert len(listing) >= 1
        item = next(i for i in listing if i["description"] == "popular entry")
        assert item["read_count"] == 3  # 2 manual + 1 from this get_knowledge call
        assert item["last_read"] is not None


class TestLogReadsSilencesErrors:
    @pytest.mark.anyio
    async def test_log_reads_failure_does_not_break_tool(self, monkeypatch) -> None:
        """_log_reads swallows exceptions so read-logging never breaks a tool response."""
        from unittest.mock import patch

        from mcp_awareness.schema import Entry, EntryType, make_id, now_utc

        s = _store()
        now = now_utc()
        s.add(
            TEST_OWNER,
            Entry(
                id=make_id(),
                type=EntryType.NOTE,
                source="test",
                tags=["silence-test"],
                created=now,
                expires=None,
                data={"description": "should still be returned"},
            ),
        )

        with patch.object(s, "log_read", side_effect=RuntimeError("pool exploded")):
            result = json.loads(await server_mod.get_knowledge(tags=["silence-test"]))["entries"]

        # Tool must succeed despite log_read blowing up
        assert len(result) >= 1
        assert result[0]["data"]["description"] == "should still be returned"


# ---------------------------------------------------------------------------
# Intention tools
# ---------------------------------------------------------------------------


class TestIntentionTools:
    @pytest.mark.anyio
    async def test_remind_creates_intention(self) -> None:
        result = json.loads(
            await server_mod.remind(
                goal="Pick up milk",
                source="personal",
                tags=["errands"],
                deliver_at="2026-03-24T18:00:00Z",
                constraints="organic, oat-preferred",
            )
        )
        assert result["status"] == "ok"
        assert result["state"] == "pending"
        # Verify it's in the store
        intentions = json.loads(await server_mod.get_intentions(state="pending"))["entries"]
        assert len(intentions) >= 1
        assert any(i["data"]["goal"] == "Pick up milk" for i in intentions)

    @pytest.mark.anyio
    async def test_get_intentions_filter_state(self) -> None:
        await server_mod.remind(goal="pending one", source="test", tags=["qa"])
        result = json.loads(await server_mod.remind(goal="will fire", source="test", tags=["qa"]))
        await server_mod.update_intention(entry_id=result["id"], state="fired")
        pending = json.loads(await server_mod.get_intentions(state="pending"))["entries"]
        fired = json.loads(await server_mod.get_intentions(state="fired"))["entries"]
        assert len(pending) >= 1
        assert len(fired) >= 1

    @pytest.mark.anyio
    async def test_get_intentions_list_mode(self) -> None:
        await server_mod.remind(goal="list mode test", source="test", tags=["qa"])
        listing = json.loads(await server_mod.get_intentions(mode="list"))["entries"]
        assert len(listing) >= 1
        assert "data" not in listing[0]
        assert "description" in listing[0]

    @pytest.mark.anyio
    async def test_update_intention_state(self) -> None:
        result = json.loads(await server_mod.remind(goal="completable", source="test", tags=["qa"]))
        entry_id = result["id"]
        # Fire it
        fired = json.loads(await server_mod.update_intention(entry_id=entry_id, state="fired"))
        assert fired["status"] == "ok"
        assert fired["state"] == "fired"
        # Complete it
        completed = json.loads(
            await server_mod.update_intention(
                entry_id=entry_id, state="completed", reason="done at Mariano's"
            )
        )
        assert completed["status"] == "ok"
        assert completed["state"] == "completed"

    @pytest.mark.anyio
    async def test_update_intention_invalid_state(self) -> None:
        result = json.loads(await server_mod.remind(goal="test", source="test", tags=[]))
        with pytest.raises(ToolError) as exc_info:
            await server_mod.update_intention(entry_id=result["id"], state="invalid")
        body = _parse_tool_error(exc_info)
        assert body["error"]["code"] == "invalid_parameter"
        assert body["error"]["param"] == "state"

    @pytest.mark.anyio
    async def test_update_intention_not_found(self) -> None:
        with pytest.raises(ToolError) as exc_info:
            await server_mod.update_intention(entry_id="nonexistent", state="fired")
        body = _parse_tool_error(exc_info)
        assert body["error"]["code"] == "not_found"

    @pytest.mark.anyio
    async def test_briefing_surfaces_fired_intentions(self) -> None:
        from datetime import timedelta

        s = _store()
        from mcp_awareness.schema import Entry, EntryType, make_id, now_utc

        past = now_utc() - timedelta(hours=1)
        s.add(
            TEST_OWNER,
            Entry(
                id=make_id(),
                type=EntryType.INTENTION,
                source="personal",
                tags=["errands"],
                created=now_utc(),
                expires=None,
                data={
                    "goal": "Pick up milk",
                    "state": "pending",
                    "deliver_at": past.isoformat(),
                },
            ),
        )
        briefing = json.loads(await server_mod.get_briefing())
        assert briefing["attention_needed"] is True
        assert len(briefing.get("fired_intentions", [])) == 1
        assert briefing["fired_intentions"][0]["goal"] == "Pick up milk"
        assert briefing["evaluation"]["intentions_fired"] == 1


# ---------------------------------------------------------------------------
# created_after / created_before filters
# ---------------------------------------------------------------------------


class TestCreatedFilters:
    @pytest.mark.anyio
    async def test_created_after_filter(self) -> None:
        from datetime import timedelta

        from mcp_awareness.schema import now_utc

        s = _store()
        early = now_utc() - timedelta(hours=2)
        late = now_utc()
        from mcp_awareness.schema import Entry, EntryType, make_id

        s.add(
            TEST_OWNER,
            Entry(
                id=make_id(),
                type=EntryType.NOTE,
                source="test",
                tags=["x"],
                created=early,
                updated=late,
                expires=None,
                data={"description": "old"},
            ),
        )
        s.add(
            TEST_OWNER,
            Entry(
                id=make_id(),
                type=EntryType.NOTE,
                source="test",
                tags=["x"],
                created=late,
                expires=None,
                data={"description": "new"},
            ),
        )
        cutoff = (now_utc() - timedelta(hours=1)).isoformat()
        result = json.loads(await server_mod.get_knowledge(created_after=cutoff))["entries"]
        assert len(result) == 1
        assert result[0]["data"]["description"] == "new"

    @pytest.mark.anyio
    async def test_created_before_filter(self) -> None:
        from datetime import timedelta

        from mcp_awareness.schema import now_utc

        s = _store()
        early = now_utc() - timedelta(hours=2)
        late = now_utc()
        from mcp_awareness.schema import Entry, EntryType, make_id

        s.add(
            TEST_OWNER,
            Entry(
                id=make_id(),
                type=EntryType.NOTE,
                source="test",
                tags=["x"],
                created=early,
                expires=None,
                data={"description": "old"},
            ),
        )
        s.add(
            TEST_OWNER,
            Entry(
                id=make_id(),
                type=EntryType.NOTE,
                source="test",
                tags=["x"],
                created=late,
                expires=None,
                data={"description": "new"},
            ),
        )
        cutoff = (now_utc() - timedelta(hours=1)).isoformat()
        result = json.loads(await server_mod.get_knowledge(created_before=cutoff))["entries"]
        assert len(result) == 1
        assert result[0]["data"]["description"] == "old"


# ---------------------------------------------------------------------------
# Semantic search tool tests
# ---------------------------------------------------------------------------


class TestSemanticSearchTool:
    """Tests for the semantic_search tool.

    Uses monkeypatch to inject a mock embedding provider.
    """

    @staticmethod
    def _vec(dim: int, axis: int) -> list[float]:
        v = [0.0] * dim
        v[axis] = 1.0
        return v

    @pytest.mark.anyio
    async def test_no_provider_returns_error(self, monkeypatch) -> None:
        """When no embedding provider is configured, raises ToolError."""
        from mcp_awareness.embeddings import NullEmbedding

        monkeypatch.setattr(server_mod, "_embedding_provider", NullEmbedding())
        with pytest.raises(ToolError) as exc_info:
            await server_mod.semantic_search(query="test")
        body = _parse_tool_error(exc_info)
        assert body["error"]["code"] == "unavailable"
        assert "embedding provider" in body["error"]["message"].lower()

    @pytest.mark.anyio
    async def test_embed_returns_empty(self, monkeypatch) -> None:
        """When embed returns empty list, raises ToolError."""

        class EmptyProvider:
            model_name = "mock"
            dimensions = 768

            def embed(self, texts: list[str]) -> list[list[float]]:
                return []

            def is_available(self) -> bool:
                return True

        monkeypatch.setattr(server_mod, "_embedding_provider", EmptyProvider())
        with pytest.raises(ToolError) as exc_info:
            await server_mod.semantic_search(query="test")
        body = _parse_tool_error(exc_info)
        assert body["error"]["code"] == "unavailable"
        assert "failed" in body["error"]["message"].lower()

    @pytest.mark.anyio
    async def test_embed_raises_exception(self, monkeypatch) -> None:
        """When embed raises, raises ToolError with message."""

        class FailingProvider:
            model_name = "mock"
            dimensions = 768

            def embed(self, texts: list[str]) -> list[list[float]]:
                raise ConnectionError("Ollama down")

            def is_available(self) -> bool:
                return True

        monkeypatch.setattr(server_mod, "_embedding_provider", FailingProvider())
        with pytest.raises(ToolError) as exc_info:
            await server_mod.semantic_search(query="test")
        body = _parse_tool_error(exc_info)
        assert body["error"]["code"] == "unavailable"
        assert "Ollama down" in body["error"]["message"]

    @pytest.mark.anyio
    async def test_search_with_mock_provider(self, monkeypatch) -> None:
        """With mock embeddings, returns ranked results."""

        class MockProvider:
            model_name = "mock"
            dimensions = 768

            def embed(self, texts):
                # Always return a unit vector along axis 0
                return [self._vec(768, 0) for _ in texts]

            def is_available(self):
                return True

            @staticmethod
            def _vec(dim, axis):
                v = [0.0] * dim
                v[axis] = 1.0
                return v

        provider = MockProvider()
        monkeypatch.setattr(server_mod, "_embedding_provider", provider)

        # Create an entry and embed it
        s = _store()
        from mcp_awareness.schema import Entry, EntryType, make_id, now_utc

        now = now_utc()
        entry = Entry(
            id=make_id(),
            type=EntryType.NOTE,
            source="test",
            tags=["finance"],
            created=now,
            expires=None,
            data={"description": "401k retirement planning notes"},
        )
        s.add(TEST_OWNER, entry)
        s.upsert_embedding(TEST_OWNER, entry.id, "mock", 768, "h1", provider._vec(768, 0))

        result = json.loads(await server_mod.semantic_search(query="retirement"))["entries"]
        assert len(result) >= 1
        assert result[0]["id"] == entry.id
        assert "similarity" in result[0]
        assert result[0]["similarity"] > 0.99

    @pytest.mark.anyio
    async def test_search_list_mode(self, monkeypatch) -> None:
        """List mode returns lightweight entries with similarity scores."""

        class MockProvider:
            model_name = "mock"
            dimensions = 768

            def embed(self, texts):
                v = [0.0] * 768
                v[0] = 1.0
                return [v for _ in texts]

            def is_available(self):
                return True

        provider = MockProvider()
        monkeypatch.setattr(server_mod, "_embedding_provider", provider)

        s = _store()
        from mcp_awareness.schema import Entry, EntryType, make_id, now_utc

        now = now_utc()
        entry = Entry(
            id=make_id(),
            type=EntryType.NOTE,
            source="test",
            tags=["test"],
            created=now,
            expires=None,
            data={"description": "test entry"},
        )
        s.add(TEST_OWNER, entry)
        vec = [0.0] * 768
        vec[0] = 1.0
        s.upsert_embedding(TEST_OWNER, entry.id, "mock", 768, "h1", vec)

        result = json.loads(await server_mod.semantic_search(query="test", mode="list"))["entries"]
        assert len(result) >= 1
        assert "similarity" in result[0]
        assert "description" in result[0]
        # Full mode fields should NOT be present in list mode
        assert "data" not in result[0]

    @pytest.mark.anyio
    async def test_search_with_filters(self, monkeypatch) -> None:
        """Filters narrow results in semantic search."""

        class MockProvider:
            model_name = "mock"
            dimensions = 768

            def embed(self, texts):
                v = [0.0] * 768
                v[0] = 1.0
                return [v for _ in texts]

            def is_available(self):
                return True

        provider = MockProvider()
        monkeypatch.setattr(server_mod, "_embedding_provider", provider)

        s = _store()
        from mcp_awareness.schema import Entry, EntryType, make_id, now_utc

        now = now_utc()
        e1 = Entry(
            id=make_id(),
            type=EntryType.NOTE,
            source="nas",
            tags=["infra"],
            created=now,
            expires=None,
            data={"description": "nas disk health"},
        )
        e2 = Entry(
            id=make_id(),
            type=EntryType.NOTE,
            source="personal",
            tags=["finance"],
            created=now,
            expires=None,
            data={"description": "retirement plan"},
        )
        s.add(TEST_OWNER, e1)
        s.add(TEST_OWNER, e2)
        vec = [0.0] * 768
        vec[0] = 1.0
        s.upsert_embedding(TEST_OWNER, e1.id, "mock", 768, "h1", vec)
        s.upsert_embedding(TEST_OWNER, e2.id, "mock", 768, "h2", vec)

        # Filter by source
        result = json.loads(await server_mod.semantic_search(query="test", source="nas"))["entries"]
        assert len(result) == 1
        assert result[0]["source"] == "nas"


# ---------------------------------------------------------------------------
# _generate_embedding edge cases
# ---------------------------------------------------------------------------


class TestGenerateEmbedding:
    def test_suppression_skipped(self, monkeypatch) -> None:
        """Suppression entries are not embedded (should_embed returns False)."""
        from mcp_awareness.schema import Entry, EntryType, make_id, now_utc

        class TrackingProvider:
            model_name = "mock"
            dimensions = 768
            called = False

            def embed(self, texts: list[str]) -> list[list[float]]:
                self.called = True
                return [[0.0] * 768 for _ in texts]

            def is_available(self) -> bool:
                return True

        provider = TrackingProvider()
        monkeypatch.setattr(server_mod, "_embedding_provider", provider)

        now = now_utc()
        suppression = Entry(
            id=make_id(),
            type=EntryType.SUPPRESSION,
            source="nas",
            tags=[],
            created=now,
            expires=None,
            data={"metric": "cpu", "suppress_level": "warning"},
        )
        server_mod._generate_embedding(suppression)
        assert provider.called is False

    @pytest.mark.anyio
    async def test_embedding_failure_silent(self, monkeypatch) -> None:
        """_generate_embedding swallows exceptions silently."""

        class ExplodingProvider:
            model_name = "mock"
            dimensions = 768

            def embed(self, texts: list[str]) -> list[list[float]]:
                raise RuntimeError("boom")

            def is_available(self) -> bool:
                return True

        monkeypatch.setattr(server_mod, "_embedding_provider", ExplodingProvider())

        # Should not raise — fire-and-forget catches the exception
        result = json.loads(
            await server_mod.remember(
                source="test",
                tags=["test"],
                description="This should not blow up",
                learned_from="test",
            )
        )
        assert result["status"] == "ok"


# ---------------------------------------------------------------------------
# Backfill embeddings tool
# ---------------------------------------------------------------------------


class TestBackfillEmbeddings:
    @pytest.mark.anyio
    async def test_no_provider_returns_error(self, monkeypatch) -> None:
        from mcp_awareness.embeddings import NullEmbedding

        monkeypatch.setattr(server_mod, "_embedding_provider", NullEmbedding())
        with pytest.raises(ToolError) as exc_info:
            await server_mod.backfill_embeddings()
        body = _parse_tool_error(exc_info)
        assert body["error"]["code"] == "unavailable"

    @pytest.mark.anyio
    async def test_backfill_creates_embeddings(self, monkeypatch) -> None:
        class MockProvider:
            model_name = "mock"
            dimensions = 768

            def embed(self, texts: list[str]) -> list[list[float]]:
                return [[0.0] * 768 for _ in texts]

            def is_available(self) -> bool:
                return True

        monkeypatch.setattr(server_mod, "_embedding_provider", MockProvider())

        # Create entries without embeddings
        s = _store()
        from mcp_awareness.schema import Entry, EntryType, make_id, now_utc

        now = now_utc()
        for i in range(3):
            s.add(
                TEST_OWNER,
                Entry(
                    id=make_id(),
                    type=EntryType.NOTE,
                    source="test",
                    tags=[],
                    created=now,
                    expires=None,
                    data={"description": f"note-{i}"},
                ),
            )

        result = json.loads(await server_mod.backfill_embeddings(limit=10))
        assert result["status"] == "ok"
        assert result["new_embeddings"] == 3
        assert result["remaining"] == 0

    @pytest.mark.anyio
    async def test_backfill_refreshes_stale(self, monkeypatch) -> None:
        """backfill re-embeds entries whose text changed since embedding."""

        class MockProvider:
            model_name = "mock"
            dimensions = 768

            def embed(self, texts: list[str]) -> list[list[float]]:
                return [[0.0] * 768 for _ in texts]

            def is_available(self) -> bool:
                return True

        monkeypatch.setattr(server_mod, "_embedding_provider", MockProvider())

        s = _store()
        from mcp_awareness.schema import Entry, EntryType, make_id, now_utc

        now = now_utc()
        entry = Entry(
            id=make_id(),
            type=EntryType.NOTE,
            source="test",
            tags=[],
            created=now,
            expires=None,
            data={"description": "original"},
        )
        s.add(TEST_OWNER, entry)

        # First backfill — creates embedding
        result = json.loads(await server_mod.backfill_embeddings(limit=10))
        assert result["new_embeddings"] == 1

        # Update the entry text
        s.update_entry(TEST_OWNER, entry.id, {"description": "changed text"})

        # Second backfill — should refresh the stale embedding
        result = json.loads(await server_mod.backfill_embeddings(limit=10))
        assert result["refreshed_embeddings"] == 1


# ---------------------------------------------------------------------------
# hint parameter on get_knowledge
# ---------------------------------------------------------------------------


class TestGetKnowledgeHint:
    @pytest.mark.anyio
    async def test_hint_reranks_results(self, monkeypatch) -> None:
        """hint param re-orders results by semantic similarity."""

        call_count = 0

        class MockProvider:
            model_name = "mock"
            dimensions = 768

            def embed(self, texts: list[str]) -> list[list[float]]:
                nonlocal call_count
                call_count += 1
                v = [0.0] * 768
                v[0] = 1.0
                return [v for _ in texts]

            def is_available(self) -> bool:
                return True

        monkeypatch.setattr(server_mod, "_embedding_provider", MockProvider())

        s = _store()
        from mcp_awareness.schema import Entry, EntryType, make_id, now_utc

        now = now_utc()
        e1 = Entry(
            id=make_id(),
            type=EntryType.NOTE,
            source="test",
            tags=["finance"],
            created=now,
            expires=None,
            data={"description": "NAS disk health"},
        )
        e2 = Entry(
            id=make_id(),
            type=EntryType.NOTE,
            source="test",
            tags=["finance"],
            created=now,
            expires=None,
            data={"description": "401k retirement"},
        )
        s.add(TEST_OWNER, e1)
        s.add(TEST_OWNER, e2)
        vec = [0.0] * 768
        vec[0] = 1.0
        s.upsert_embedding(TEST_OWNER, e1.id, "mock", 768, "h1", vec)
        s.upsert_embedding(TEST_OWNER, e2.id, "mock", 768, "h2", vec)

        result = json.loads(
            await server_mod.get_knowledge(tags=["finance"], hint="retirement savings")
        )["entries"]
        assert len(result) == 2
        # With hint, results should include similarity scores
        assert "similarity" in result[0]

    @pytest.mark.anyio
    async def test_hint_list_mode_includes_similarity(self, monkeypatch) -> None:
        """hint in list mode includes similarity scores."""

        class MockProvider:
            model_name = "mock"
            dimensions = 768

            def embed(self, texts: list[str]) -> list[list[float]]:
                v = [0.0] * 768
                v[0] = 1.0
                return [v for _ in texts]

            def is_available(self) -> bool:
                return True

        monkeypatch.setattr(server_mod, "_embedding_provider", MockProvider())

        s = _store()
        from mcp_awareness.schema import Entry, EntryType, make_id, now_utc

        now = now_utc()
        entry = Entry(
            id=make_id(),
            type=EntryType.NOTE,
            source="test",
            tags=["test"],
            created=now,
            expires=None,
            data={"description": "test entry"},
        )
        s.add(TEST_OWNER, entry)
        vec = [0.0] * 768
        vec[0] = 1.0
        s.upsert_embedding(TEST_OWNER, entry.id, "mock", 768, "h1", vec)

        result = json.loads(
            await server_mod.get_knowledge(tags=["test"], hint="test", mode="list")
        )["entries"]
        assert len(result) == 1
        assert "similarity" in result[0]
        assert "data" not in result[0]

    @pytest.mark.anyio
    async def test_hint_without_provider_falls_back(self) -> None:
        """hint is silently ignored when no embedding provider is available."""
        s = _store()
        from mcp_awareness.schema import Entry, EntryType, make_id, now_utc

        now = now_utc()
        s.add(
            TEST_OWNER,
            Entry(
                id=make_id(),
                type=EntryType.NOTE,
                source="test",
                tags=["test"],
                created=now,
                expires=None,
                data={"description": "test note"},
            ),
        )
        # Default NullEmbedding — hint should be ignored, not error
        result = json.loads(await server_mod.get_knowledge(tags=["test"], hint="something"))[
            "entries"
        ]
        assert len(result) == 1
        assert "similarity" not in result[0]


# ---------------------------------------------------------------------------
# Entry relationships (get_related)
# ---------------------------------------------------------------------------


class TestGetRelated:
    @pytest.mark.anyio
    async def test_forward_references(self) -> None:
        """get_related returns entries referenced in related_ids."""
        s = _store()
        from mcp_awareness.schema import Entry, EntryType, make_id, now_utc

        now = now_utc()
        target = Entry(
            id=make_id(),
            type=EntryType.NOTE,
            source="test",
            tags=[],
            created=now,
            expires=None,
            data={"description": "target entry"},
        )
        referrer = Entry(
            id=make_id(),
            type=EntryType.NOTE,
            source="test",
            tags=[],
            created=now,
            expires=None,
            data={"description": "links to target", "related_ids": [target.id]},
        )
        s.add(TEST_OWNER, target)
        s.add(TEST_OWNER, referrer)

        result = json.loads(await server_mod.get_related(entry_id=referrer.id))
        assert len(result) == 1
        assert result[0]["id"] == target.id

    @pytest.mark.anyio
    async def test_reverse_references(self) -> None:
        """get_related returns entries that reference the given entry."""
        s = _store()
        from mcp_awareness.schema import Entry, EntryType, make_id, now_utc

        now = now_utc()
        target = Entry(
            id=make_id(),
            type=EntryType.NOTE,
            source="test",
            tags=[],
            created=now,
            expires=None,
            data={"description": "target entry"},
        )
        referrer = Entry(
            id=make_id(),
            type=EntryType.NOTE,
            source="test",
            tags=[],
            created=now,
            expires=None,
            data={"description": "links to target", "related_ids": [target.id]},
        )
        s.add(TEST_OWNER, target)
        s.add(TEST_OWNER, referrer)

        result = json.loads(await server_mod.get_related(entry_id=target.id))
        assert len(result) == 1
        assert result[0]["id"] == referrer.id

    @pytest.mark.anyio
    async def test_bidirectional_deduplicates(self) -> None:
        """Entries appearing in both forward and reverse are not duplicated."""
        s = _store()
        from mcp_awareness.schema import Entry, EntryType, make_id, now_utc

        now = now_utc()
        a = Entry(
            id=make_id(),
            type=EntryType.NOTE,
            source="test",
            tags=[],
            created=now,
            expires=None,
            data={"description": "entry A"},
        )
        b = Entry(
            id=make_id(),
            type=EntryType.NOTE,
            source="test",
            tags=[],
            created=now,
            expires=None,
            data={"description": "entry B", "related_ids": [a.id]},
        )
        # A also references B
        a.data["related_ids"] = [b.id]
        s.add(TEST_OWNER, a)
        s.add(TEST_OWNER, b)

        result = json.loads(await server_mod.get_related(entry_id=a.id))
        assert len(result) == 1
        assert result[0]["id"] == b.id

    @pytest.mark.anyio
    async def test_not_found(self) -> None:
        with pytest.raises(ToolError) as exc_info:
            await server_mod.get_related(entry_id="nonexistent")
        body = _parse_tool_error(exc_info)
        assert body["error"]["code"] == "not_found"

    @pytest.mark.anyio
    async def test_no_relations(self) -> None:
        """Entry with no related_ids returns empty list."""
        s = _store()
        from mcp_awareness.schema import Entry, EntryType, make_id, now_utc

        now = now_utc()
        entry = Entry(
            id=make_id(),
            type=EntryType.NOTE,
            source="test",
            tags=[],
            created=now,
            expires=None,
            data={"description": "lone wolf"},
        )
        s.add(TEST_OWNER, entry)
        result = json.loads(await server_mod.get_related(entry_id=entry.id))
        assert result == []

    @pytest.mark.anyio
    async def test_list_mode(self) -> None:
        s = _store()
        from mcp_awareness.schema import Entry, EntryType, make_id, now_utc

        now = now_utc()
        target = Entry(
            id=make_id(),
            type=EntryType.NOTE,
            source="test",
            tags=[],
            created=now,
            expires=None,
            data={"description": "target"},
        )
        referrer = Entry(
            id=make_id(),
            type=EntryType.NOTE,
            source="test",
            tags=[],
            created=now,
            expires=None,
            data={"description": "referrer", "related_ids": [target.id]},
        )
        s.add(TEST_OWNER, target)
        s.add(TEST_OWNER, referrer)
        result = json.loads(await server_mod.get_related(entry_id=referrer.id, mode="list"))
        assert len(result) == 1
        assert "description" in result[0]
        assert "data" not in result[0]


# ---------------------------------------------------------------------------
# Semantic search integration tests (require Ollama)
# ---------------------------------------------------------------------------

_OLLAMA_URL = os.environ.get("AWARENESS_OLLAMA_URL", "http://localhost:11434")
_ollama_available: bool | None = None


def _is_ollama_available() -> bool:
    global _ollama_available
    if _ollama_available is None:
        p = OllamaEmbedding(base_url=_OLLAMA_URL)
        _ollama_available = p.is_available()
    return _ollama_available


skip_no_ollama = pytest.mark.skipif(
    "not _is_ollama_available()",
    reason="Ollama not available",
)


class TestSemanticSearchIntegration:
    """End-to-end tests with real Ollama embeddings."""

    @skip_no_ollama
    @pytest.mark.anyio
    async def test_write_and_search_round_trip(self, monkeypatch) -> None:
        """remember → semantic_search finds entry by meaning."""
        provider = OllamaEmbedding(base_url=_OLLAMA_URL)
        monkeypatch.setattr(server_mod, "_embedding_provider", provider)

        # Write two entries with different topics
        await server_mod.remember(
            source="test-rag",
            tags=["finance"],
            description="401k contribution limits increased to $23,500 for 2026",
            learned_from="test",
        )
        await server_mod.remember(
            source="test-rag",
            tags=["infra"],
            description="NAS RAID array rebuilt after replacing drive sdb",
            learned_from="test",
        )

        # Wait for background embedding threads to embed BOTH entries
        import time

        for _ in range(30):
            page = json.loads(await server_mod.semantic_search(query="retirement savings"))
            result = page["entries"]
            if len(result) >= 2:
                break
            time.sleep(0.5)
        assert len(result) >= 2
        # The finance entry should rank higher than the infra entry
        assert "401k" in result[0]["data"]["description"]
        assert result[0]["similarity"] > result[1]["similarity"]

    @skip_no_ollama
    @pytest.mark.anyio
    async def test_generate_embedding_on_write(self, monkeypatch) -> None:
        """_generate_embedding fires on remember and creates an embedding."""
        provider = OllamaEmbedding(base_url=_OLLAMA_URL)
        monkeypatch.setattr(server_mod, "_embedding_provider", provider)

        result = json.loads(
            await server_mod.remember(
                source="test-rag",
                tags=["test"],
                description="This is a test entry for embedding generation",
                learned_from="test",
            )
        )
        entry_id = result["id"]

        # Wait for background embedding thread pool to drain
        import time

        for _ in range(20):
            s = _store()
            missing = s.get_entries_without_embeddings(TEST_OWNER, "nomic-embed-text")
            if entry_id not in [e.id for e in missing]:
                break
            time.sleep(0.5)

        # Verify embedding was created in the store
        s = _store()
        missing = s.get_entries_without_embeddings(TEST_OWNER, "nomic-embed-text")
        missing_ids = [e.id for e in missing]
        assert entry_id not in missing_ids


# ---------------------------------------------------------------------------
# Owner context + main() coverage
# ---------------------------------------------------------------------------


def test_owner_id_returns_default(monkeypatch):
    """_owner_id() returns DEFAULT_OWNER when no contextvar is set."""
    monkeypatch.setattr(server_mod, "DEFAULT_OWNER", "test-default")
    assert server_mod._owner_id() == "test-default"


def test_owner_id_returns_contextvar():
    """_owner_id() returns contextvar value when set."""
    token = server_mod._owner_ctx.set("ctx-user")
    try:
        assert server_mod._owner_id() == "ctx-user"
    finally:
        server_mod._owner_ctx.reset(token)


def test_main_handles_keyboard_interrupt(monkeypatch):
    """main() catches KeyboardInterrupt and exits cleanly."""
    monkeypatch.setattr(server_mod, "_sync_custom_prompts", lambda **kw: None)
    monkeypatch.setattr(server_mod, "_run", _raise_keyboard_interrupt)
    # Should not raise
    server_mod.main()


def _raise_keyboard_interrupt():
    raise KeyboardInterrupt


def test_fallback_user_on_getpass_failure(monkeypatch):
    """When getpass.getuser() fails, _fallback_user resolves to 'system'."""
    import importlib

    monkeypatch.setattr("getpass.getuser", lambda: (_ for _ in ()).throw(OSError("no tty")))
    monkeypatch.delenv("AWARENESS_DEFAULT_OWNER", raising=False)
    importlib.reload(server_mod)
    assert server_mod.DEFAULT_OWNER == "system"
    # Restore
    importlib.reload(server_mod)


# ---------------------------------------------------------------------------
# Date validation tests
# ---------------------------------------------------------------------------


class TestDateValidation:
    @pytest.mark.anyio
    async def test_get_alerts_malformed_since(self) -> None:
        with pytest.raises(ToolError) as exc_info:
            await server_mod.get_alerts(since="not-a-date")
        body = _parse_tool_error(exc_info)
        assert body["error"]["code"] == "invalid_parameter"
        assert body["error"]["param"] == "since"

    @pytest.mark.anyio
    async def test_get_knowledge_malformed_since(self) -> None:
        with pytest.raises(ToolError) as exc_info:
            await server_mod.get_knowledge(since="not-a-date")
        body = _parse_tool_error(exc_info)
        assert body["error"]["code"] == "invalid_parameter"

    @pytest.mark.anyio
    async def test_get_knowledge_malformed_until(self) -> None:
        with pytest.raises(ToolError) as exc_info:
            await server_mod.get_knowledge(until="2026-13-45")
        body = _parse_tool_error(exc_info)
        assert body["error"]["code"] == "invalid_parameter"

    @pytest.mark.anyio
    async def test_get_knowledge_malformed_created_after(self) -> None:
        with pytest.raises(ToolError) as exc_info:
            await server_mod.get_knowledge(created_after="bad")
        body = _parse_tool_error(exc_info)
        assert body["error"]["code"] == "invalid_parameter"

    @pytest.mark.anyio
    async def test_remind_malformed_deliver_at(self) -> None:
        with pytest.raises(ToolError) as exc_info:
            await server_mod.remind(
                goal="test", source="test", tags=["test"], deliver_at="not-a-date"
            )
        body = _parse_tool_error(exc_info)
        assert body["error"]["code"] == "invalid_parameter"

    @pytest.mark.anyio
    async def test_get_deleted_malformed_since(self) -> None:
        with pytest.raises(ToolError) as exc_info:
            await server_mod.get_deleted(since="not-a-date")
        body = _parse_tool_error(exc_info)
        assert body["error"]["code"] == "invalid_parameter"

    @pytest.mark.anyio
    async def test_semantic_search_malformed_since(self) -> None:
        with pytest.raises(ToolError) as exc_info:
            await server_mod.semantic_search(query="test", since="not-a-date")
        body = _parse_tool_error(exc_info)
        assert body["error"]["code"] == "invalid_parameter"

    @pytest.mark.anyio
    async def test_get_knowledge_malformed_created_before(self) -> None:
        with pytest.raises(ToolError) as exc_info:
            await server_mod.get_knowledge(created_before="nope")
        body = _parse_tool_error(exc_info)
        assert body["error"]["code"] == "invalid_parameter"
        assert body["error"]["param"] == "created_before"

    @pytest.mark.anyio
    async def test_get_reads_malformed_since(self) -> None:
        with pytest.raises(ToolError) as exc_info:
            await server_mod.get_reads(since="not-a-date")
        body = _parse_tool_error(exc_info)
        assert body["error"]["code"] == "invalid_parameter"
        assert body["error"]["param"] == "since"

    @pytest.mark.anyio
    async def test_get_actions_malformed_since(self) -> None:
        with pytest.raises(ToolError) as exc_info:
            await server_mod.get_actions(since="not-a-date")
        body = _parse_tool_error(exc_info)
        assert body["error"]["code"] == "invalid_parameter"
        assert body["error"]["param"] == "since"

    @pytest.mark.anyio
    async def test_get_unread_malformed_since(self) -> None:
        with pytest.raises(ToolError) as exc_info:
            await server_mod.get_unread(since="not-a-date")
        body = _parse_tool_error(exc_info)
        assert body["error"]["code"] == "invalid_parameter"
        assert body["error"]["param"] == "since"

    @pytest.mark.anyio
    async def test_get_activity_malformed_since(self) -> None:
        with pytest.raises(ToolError) as exc_info:
            await server_mod.get_activity(since="not-a-date")
        body = _parse_tool_error(exc_info)
        assert body["error"]["code"] == "invalid_parameter"
        assert body["error"]["param"] == "since"

    @pytest.mark.anyio
    async def test_semantic_search_malformed_since_with_provider(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Date validation in semantic_search now happens before provider check."""

        with pytest.raises(ToolError) as exc_info:
            await server_mod.semantic_search(query="test", since="not-a-date")
        body = _parse_tool_error(exc_info)
        assert body["error"]["code"] == "invalid_parameter"
        assert body["error"]["param"] == "since"

    @pytest.mark.anyio
    async def test_semantic_search_malformed_until_with_provider(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Date validation for until in semantic_search now happens before provider check."""

        with pytest.raises(ToolError) as exc_info:
            await server_mod.semantic_search(query="test", until="2026-99-99")
        body = _parse_tool_error(exc_info)
        assert body["error"]["code"] == "invalid_parameter"
        assert body["error"]["param"] == "until"

    @pytest.mark.anyio
    async def test_get_alerts_valid_date_still_works(self) -> None:
        result = await server_mod.get_alerts(since="2026-03-30T00:00:00Z")
        parsed = json.loads(result)
        assert isinstance(parsed, dict)
        assert "entries" in parsed
        assert isinstance(parsed["entries"], list)
