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

"""Integration tests for schema/record MCP tool handlers.

Uses testcontainers Postgres + direct tool-function calls via the server's
_owner_id / store accessors (both monkeypatched for tests).
"""

from __future__ import annotations

import json

import pytest

from mcp_awareness.schema import EntryType

TEST_OWNER = "test-owner"


@pytest.fixture
def configured_server(store, monkeypatch):
    """Wire the FastMCP server-module helpers to the testcontainers store and owner."""
    import mcp_awareness.server as srv

    monkeypatch.setattr(srv, "store", store)
    monkeypatch.setattr(srv, "_owner_id", lambda: TEST_OWNER)
    yield srv


def _parse_tool_error(excinfo):
    """Parse the structured JSON envelope from a ToolError."""
    return json.loads(str(excinfo.value))


@pytest.mark.asyncio
async def test_register_schema_happy_path(configured_server):
    from mcp_awareness.tools import register_schema

    response = await register_schema(
        source="test",
        tags=["schema"],
        description="test schema",
        family="schema:test-thing",
        version="1.0.0",
        schema={"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
    )
    body = json.loads(response)
    assert body["status"] == "ok"
    assert body["logical_key"] == "schema:test-thing:1.0.0"
    assert "id" in body


@pytest.mark.asyncio
async def test_register_schema_rejects_invalid_schema(configured_server):
    from mcp.server.fastmcp.exceptions import ToolError

    from mcp_awareness.tools import register_schema

    with pytest.raises(ToolError) as excinfo:
        await register_schema(
            source="test",
            tags=[],
            description="bad schema",
            family="schema:bad",
            version="1.0.0",
            schema={"type": "strng"},  # typo — not a valid JSON Schema type
        )
    err = _parse_tool_error(excinfo)["error"]
    assert err["code"] == "invalid_schema"


@pytest.mark.asyncio
async def test_register_schema_rejects_duplicate_family_version(configured_server):
    from mcp.server.fastmcp.exceptions import ToolError

    from mcp_awareness.tools import register_schema

    await register_schema(
        source="test",
        tags=[],
        description="v1",
        family="schema:dup",
        version="1.0.0",
        schema={"type": "object"},
    )
    with pytest.raises(ToolError) as excinfo:
        await register_schema(
            source="test",
            tags=[],
            description="v1 again",
            family="schema:dup",
            version="1.0.0",
            schema={"type": "object"},
        )
    err = _parse_tool_error(excinfo)["error"]
    assert err["code"] == "schema_already_exists"


@pytest.mark.asyncio
async def test_register_schema_rejects_empty_family(configured_server):
    from mcp.server.fastmcp.exceptions import ToolError

    from mcp_awareness.tools import register_schema

    with pytest.raises(ToolError) as excinfo:
        await register_schema(
            source="test",
            tags=[],
            description="bad",
            family="",
            version="1.0.0",
            schema={"type": "object"},
        )
    err = _parse_tool_error(excinfo)["error"]
    assert err["code"] == "invalid_parameter"


@pytest.mark.asyncio
async def test_register_schema_rejects_empty_version(configured_server):
    from mcp.server.fastmcp.exceptions import ToolError

    from mcp_awareness.tools import register_schema

    with pytest.raises(ToolError) as excinfo:
        await register_schema(
            source="test",
            tags=[],
            description="bad",
            family="schema:test",
            version="",
            schema={"type": "object"},
        )
    err = _parse_tool_error(excinfo)["error"]
    assert err["code"] == "invalid_parameter"


@pytest.mark.asyncio
async def test_create_record_happy_path(configured_server):
    from mcp_awareness.tools import create_record, register_schema

    await register_schema(
        source="test",
        tags=[],
        description="s",
        family="schema:thing",
        version="1.0.0",
        schema={"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
    )
    response = await create_record(
        source="test",
        tags=[],
        description="a thing",
        logical_key="thing-one",
        schema_ref="schema:thing",
        schema_version="1.0.0",
        content={"name": "widget"},
    )
    body = json.loads(response)
    assert body["status"] == "ok"
    assert body["action"] == "created"
    assert "id" in body


@pytest.mark.asyncio
async def test_create_record_rejects_unknown_schema(configured_server):
    from mcp.server.fastmcp.exceptions import ToolError

    from mcp_awareness.tools import create_record

    with pytest.raises(ToolError) as excinfo:
        await create_record(
            source="test",
            tags=[],
            description="orphan",
            logical_key="thing-one",
            schema_ref="schema:does-not-exist",
            schema_version="1.0.0",
            content={"name": "widget"},
        )
    err = json.loads(str(excinfo.value))["error"]
    assert err["code"] == "schema_not_found"
    assert err["searched_owners"] == [TEST_OWNER, "_system"]


@pytest.mark.asyncio
async def test_create_record_surfaces_validation_errors(configured_server):
    from mcp.server.fastmcp.exceptions import ToolError

    from mcp_awareness.tools import create_record, register_schema

    await register_schema(
        source="test",
        tags=[],
        description="s",
        family="schema:person",
        version="1.0.0",
        schema={
            "type": "object",
            "properties": {"name": {"type": "string"}, "age": {"type": "integer"}},
            "required": ["name"],
        },
    )
    with pytest.raises(ToolError) as excinfo:
        await create_record(
            source="test",
            tags=[],
            description="bad person",
            logical_key="p1",
            schema_ref="schema:person",
            schema_version="1.0.0",
            content={"age": "thirty"},
        )
    err = json.loads(str(excinfo.value))["error"]
    assert err["code"] == "validation_failed"
    validators = {ve["validator"] for ve in err["validation_errors"]}
    assert "required" in validators
    assert "type" in validators


@pytest.mark.asyncio
async def test_create_record_upsert_on_same_logical_key(configured_server):
    from mcp_awareness.tools import create_record, register_schema

    await register_schema(
        source="test",
        tags=[],
        description="s",
        family="schema:thing",
        version="1.0.0",
        schema={"type": "object"},
    )
    r1 = json.loads(
        await create_record(
            source="test",
            tags=[],
            description="v1",
            logical_key="thing-one",
            schema_ref="schema:thing",
            schema_version="1.0.0",
            content={"v": 1},
        )
    )
    assert r1["action"] == "created"
    r2 = json.loads(
        await create_record(
            source="test",
            tags=[],
            description="v2",
            logical_key="thing-one",
            schema_ref="schema:thing",
            schema_version="1.0.0",
            content={"v": 2},
        )
    )
    assert r2["action"] == "updated"
    assert r2["id"] == r1["id"]


@pytest.mark.asyncio
async def test_create_record_uses_system_schema_fallback(configured_server, store):
    """A record can reference a schema owned by _system, not the caller."""
    from mcp_awareness.schema import Entry, make_id, now_utc
    from mcp_awareness.tools import create_record

    # Seed _system schema directly via store (not via tool — tool writes caller's owner)
    store.add(
        "_system",
        Entry(
            id=make_id(),
            type=EntryType.SCHEMA,
            source="system",
            tags=["system"],
            created=now_utc(),
            expires=None,
            data={
                "family": "schema:system-thing",
                "version": "1.0.0",
                "schema": {"type": "object"},
                "description": "system-seeded",
                "learned_from": "cli-bootstrap",
            },
            logical_key="schema:system-thing:1.0.0",
        ),
    )

    response = await create_record(
        source="test",
        tags=[],
        description="mine",
        logical_key="mine-1",
        schema_ref="schema:system-thing",
        schema_version="1.0.0",
        content={"any": "thing"},
    )
    body = json.loads(response)
    assert body["status"] == "ok"
    assert body["action"] == "created"


@pytest.mark.asyncio
async def test_update_entry_rejects_schema_update(configured_server):
    from mcp.server.fastmcp.exceptions import ToolError

    from mcp_awareness.tools import register_schema, update_entry

    resp = json.loads(
        await register_schema(
            source="test",
            tags=[],
            description="s",
            family="schema:thing",
            version="1.0.0",
            schema={"type": "object"},
        )
    )
    with pytest.raises(ToolError) as excinfo:
        await update_entry(entry_id=resp["id"], description="new desc")
    err = json.loads(str(excinfo.value))["error"]
    assert err["code"] == "schema_immutable"


@pytest.mark.asyncio
async def test_update_entry_record_content_revalidates_valid(configured_server):
    from mcp_awareness.tools import create_record, register_schema, update_entry

    await register_schema(
        source="test",
        tags=[],
        description="s",
        family="schema:thing",
        version="1.0.0",
        schema={"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
    )
    r = json.loads(
        await create_record(
            source="test",
            tags=[],
            description="r",
            logical_key="r1",
            schema_ref="schema:thing",
            schema_version="1.0.0",
            content={"name": "good"},
        )
    )
    # Valid content update — passes re-validation
    await update_entry(entry_id=r["id"], content={"name": "still-good"})


@pytest.mark.asyncio
async def test_update_entry_record_content_rejects_invalid(configured_server):
    from mcp.server.fastmcp.exceptions import ToolError

    from mcp_awareness.tools import create_record, register_schema, update_entry

    await register_schema(
        source="test",
        tags=[],
        description="s",
        family="schema:thing",
        version="1.0.0",
        schema={"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
    )
    r = json.loads(
        await create_record(
            source="test",
            tags=[],
            description="r",
            logical_key="r1",
            schema_ref="schema:thing",
            schema_version="1.0.0",
            content={"name": "good"},
        )
    )
    with pytest.raises(ToolError) as excinfo:
        await update_entry(entry_id=r["id"], content={"name": 123})
    err = json.loads(str(excinfo.value))["error"]
    assert err["code"] == "validation_failed"


@pytest.mark.asyncio
async def test_update_entry_record_non_content_skips_revalidation(configured_server):
    from mcp_awareness.tools import create_record, register_schema, update_entry

    await register_schema(
        source="test",
        tags=[],
        description="s",
        family="schema:thing",
        version="1.0.0",
        schema={"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
    )
    r = json.loads(
        await create_record(
            source="test",
            tags=[],
            description="orig",
            logical_key="r1",
            schema_ref="schema:thing",
            schema_version="1.0.0",
            content={"name": "good"},
        )
    )
    # Description-only update skips re-validation
    await update_entry(entry_id=r["id"], description="updated desc")


@pytest.mark.asyncio
async def test_delete_entry_schema_with_no_records_succeeds(configured_server):
    from mcp_awareness.tools import delete_entry, register_schema

    resp = json.loads(await register_schema(
        source="test", tags=[], description="s",
        family="schema:thing", version="1.0.0",
        schema={"type": "object"},
    ))
    # No records → soft-delete succeeds
    await delete_entry(entry_id=resp["id"])
    # Verify soft-deleted: find_schema returns None
    assert configured_server.store.find_schema(TEST_OWNER, "schema:thing:1.0.0") is None


@pytest.mark.asyncio
async def test_delete_entry_schema_with_records_rejected(configured_server):
    from mcp.server.fastmcp.exceptions import ToolError
    from mcp_awareness.tools import create_record, delete_entry, register_schema

    resp = json.loads(await register_schema(
        source="test", tags=[], description="s",
        family="schema:thing", version="1.0.0",
        schema={"type": "object"},
    ))
    await create_record(
        source="test", tags=[], description="r",
        logical_key="r1",
        schema_ref="schema:thing", schema_version="1.0.0",
        content={},
    )
    with pytest.raises(ToolError) as excinfo:
        await delete_entry(entry_id=resp["id"])
    err = json.loads(str(excinfo.value))["error"]
    assert err["code"] == "schema_in_use"
    assert len(err["referencing_records"]) == 1
    assert err["total_count"] == 1


@pytest.mark.asyncio
async def test_delete_entry_schema_allowed_after_records_deleted(configured_server):
    from mcp_awareness.tools import create_record, delete_entry, register_schema

    schema_resp = json.loads(await register_schema(
        source="test", tags=[], description="s",
        family="schema:thing", version="1.0.0",
        schema={"type": "object"},
    ))
    record_resp = json.loads(await create_record(
        source="test", tags=[], description="r",
        logical_key="r1",
        schema_ref="schema:thing", schema_version="1.0.0",
        content={},
    ))
    # Soft-delete the record first
    await delete_entry(entry_id=record_resp["id"])
    # Now schema delete succeeds
    await delete_entry(entry_id=schema_resp["id"])
