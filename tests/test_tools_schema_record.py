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


@pytest.mark.anyio
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


@pytest.mark.anyio
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


@pytest.mark.anyio
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
    # Structured extras — callers can locate the existing entry without parsing
    # the human-readable message. Matches design-doc error-code table.
    assert err["logical_key"] == "schema:dup:1.0.0"
    assert err["existing_id"]  # non-empty; first-register's entry id


@pytest.mark.anyio
async def test_register_schema_reraises_non_unique_exception(configured_server, monkeypatch):
    """register_schema re-raises generic exceptions that are not unique violations."""
    import mcp_awareness.server as srv
    from mcp_awareness.tools import register_schema

    original_add = srv.store.add

    def _fake_add(owner_id, entry):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(srv.store, "add", _fake_add)

    with pytest.raises(RuntimeError, match="connection refused"):
        await register_schema(
            source="test",
            tags=[],
            description="non-unique exception test",
            family="schema:reraise",
            version="1.0.0",
            schema={"type": "object"},
        )

    monkeypatch.setattr(srv.store, "add", original_add)


@pytest.mark.anyio
async def test_create_record_validate_raises_unexpected_error(configured_server, monkeypatch):
    """create_record reports validation_error if validate_record_content raises unexpectedly."""
    from mcp.server.fastmcp.exceptions import ToolError

    from mcp_awareness.tools import create_record, register_schema

    await register_schema(
        source="test",
        tags=[],
        description="s",
        family="schema:except",
        version="1.0.0",
        schema={"type": "object"},
    )

    import mcp_awareness.validation as validation_mod

    monkeypatch.setattr(
        validation_mod,
        "validate_record_content",
        lambda _s, _c: (_ for _ in ()).throw(RuntimeError("internal jsonschema error")),
    )

    with pytest.raises(ToolError) as excinfo:
        await create_record(
            source="test",
            tags=[],
            description="error case",
            logical_key="except-rec",
            schema_ref="schema:except",
            schema_version="1.0.0",
            content={},
        )
    err = json.loads(str(excinfo.value))["error"]
    assert err["code"] == "validation_error"
    assert "internal jsonschema error" in err["message"]


@pytest.mark.anyio
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


@pytest.mark.anyio
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


@pytest.mark.anyio
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


@pytest.mark.anyio
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


@pytest.mark.anyio
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


@pytest.mark.anyio
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


@pytest.mark.anyio
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


@pytest.mark.anyio
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


@pytest.mark.anyio
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
    # Content shape must remain native JSON (dict), not a JSON-encoded string —
    # matches the create path so downstream consumers see a stable wire shape.
    stored = configured_server.store.get_entry_by_id(TEST_OWNER, r["id"])
    assert stored is not None
    assert stored.data["content"] == {"name": "still-good"}
    assert isinstance(stored.data["content"], dict)


@pytest.mark.anyio
async def test_update_entry_record_preserves_primitive_content_shape(configured_server):
    """Primitive (int/array) record content must also keep native JSON shape after update."""
    from mcp_awareness.tools import create_record, register_schema, update_entry

    await register_schema(
        source="test",
        tags=[],
        description="s",
        family="schema:counter",
        version="1.0.0",
        schema={"type": "integer"},
    )
    r = json.loads(
        await create_record(
            source="test",
            tags=[],
            description="c",
            logical_key="c1",
            schema_ref="schema:counter",
            schema_version="1.0.0",
            content=42,
        )
    )
    await update_entry(entry_id=r["id"], content=99)
    stored = configured_server.store.get_entry_by_id(TEST_OWNER, r["id"])
    assert stored is not None
    assert stored.data["content"] == 99
    assert isinstance(stored.data["content"], int)


@pytest.mark.anyio
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


@pytest.mark.anyio
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


@pytest.mark.anyio
async def test_delete_entry_schema_with_no_records_succeeds(configured_server):
    from mcp_awareness.tools import delete_entry, register_schema

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
    # No records → soft-delete succeeds
    await delete_entry(entry_id=resp["id"])
    # Verify soft-deleted: find_schema returns None
    assert configured_server.store.find_schema(TEST_OWNER, "schema:thing:1.0.0") is None


@pytest.mark.anyio
async def test_delete_entry_schema_with_records_rejected(configured_server):
    from mcp.server.fastmcp.exceptions import ToolError

    from mcp_awareness.tools import create_record, delete_entry, register_schema

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
    await create_record(
        source="test",
        tags=[],
        description="r",
        logical_key="r1",
        schema_ref="schema:thing",
        schema_version="1.0.0",
        content={},
    )
    with pytest.raises(ToolError) as excinfo:
        await delete_entry(entry_id=resp["id"])
    err = json.loads(str(excinfo.value))["error"]
    assert err["code"] == "schema_in_use"
    assert len(err["referencing_records"]) == 1
    assert err["total_count"] == 1


@pytest.mark.anyio
async def test_delete_entry_schema_allowed_after_records_deleted(configured_server):
    from mcp_awareness.tools import create_record, delete_entry, register_schema

    schema_resp = json.loads(
        await register_schema(
            source="test",
            tags=[],
            description="s",
            family="schema:thing",
            version="1.0.0",
            schema={"type": "object"},
        )
    )
    record_resp = json.loads(
        await create_record(
            source="test",
            tags=[],
            description="r",
            logical_key="r1",
            schema_ref="schema:thing",
            schema_version="1.0.0",
            content={},
        )
    )
    # Soft-delete the record first
    await delete_entry(entry_id=record_resp["id"])
    # Now schema delete succeeds
    await delete_entry(entry_id=schema_resp["id"])


@pytest.mark.anyio
async def test_cross_owner_schema_invisible(configured_server, store, monkeypatch):
    """Owner A registers a schema; Owner B cannot resolve it."""
    from mcp.server.fastmcp.exceptions import ToolError

    import mcp_awareness.server as srv
    from mcp_awareness.tools import create_record, register_schema

    # Owner A (default TEST_OWNER) registers a schema
    await register_schema(
        source="test",
        tags=[],
        description="A's schema",
        family="schema:mine",
        version="1.0.0",
        schema={"type": "object"},
    )

    # Switch to Owner B by re-patching the _owner_id accessor
    monkeypatch.setattr(srv, "_owner_id", lambda: "other-owner")
    with pytest.raises(ToolError) as excinfo:
        await create_record(
            source="test",
            tags=[],
            description="B's attempt",
            logical_key="r-b",
            schema_ref="schema:mine",
            schema_version="1.0.0",
            content={},
        )
    err = json.loads(str(excinfo.value))["error"]
    assert err["code"] == "schema_not_found"


@pytest.mark.anyio
async def test_both_owners_see_system_schema(configured_server, store, monkeypatch):
    """Both A and B can use a _system schema."""
    import mcp_awareness.server as srv
    from mcp_awareness.schema import Entry, make_id, now_utc
    from mcp_awareness.tools import create_record

    # Seed _system schema directly
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
                "family": "schema:shared",
                "version": "1.0.0",
                "schema": {"type": "object"},
                "description": "shared",
                "learned_from": "cli-bootstrap",
            },
            logical_key="schema:shared:1.0.0",
        ),
    )

    # A creates a record against _system schema
    a_resp = json.loads(
        await create_record(
            source="test",
            tags=[],
            description="A's record",
            logical_key="rec-a",
            schema_ref="schema:shared",
            schema_version="1.0.0",
            content={"who": "alice"},
        )
    )
    assert a_resp["status"] == "ok"

    # Switch to Owner B
    monkeypatch.setattr(srv, "_owner_id", lambda: "bob")
    b_resp = json.loads(
        await create_record(
            source="test",
            tags=[],
            description="B's record",
            logical_key="rec-b",
            schema_ref="schema:shared",
            schema_version="1.0.0",
            content={"who": "bob"},
        )
    )
    assert b_resp["status"] == "ok"


@pytest.mark.anyio
async def test_caller_schema_overrides_system(configured_server, store, monkeypatch):
    """When both _system and caller have the same logical_key, caller's version wins."""

    from mcp.server.fastmcp.exceptions import ToolError

    from mcp_awareness.schema import Entry, make_id, now_utc
    from mcp_awareness.tools import create_record, register_schema

    # _system schema allows integer only
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
                "family": "schema:override",
                "version": "1.0.0",
                "schema": {"type": "integer"},
                "description": "system strict",
                "learned_from": "cli-bootstrap",
            },
            logical_key="schema:override:1.0.0",
        ),
    )

    # Caller schema allows string only — overrides _system
    await register_schema(
        source="test",
        tags=[],
        description="caller's permissive",
        family="schema:override",
        version="1.0.0",
        schema={"type": "string"},
    )

    # Caller's record with a STRING should pass (caller's schema wins)
    resp = json.loads(
        await create_record(
            source="test",
            tags=[],
            description="caller-wins",
            logical_key="rec-override",
            schema_ref="schema:override",
            schema_version="1.0.0",
            content="string-value",
        )
    )
    assert resp["status"] == "ok"

    # Caller's record with an INTEGER should FAIL (caller's schema says string only)
    with pytest.raises(ToolError) as excinfo:
        await create_record(
            source="test",
            tags=[],
            description="wrong-type",
            logical_key="rec-override-2",
            schema_ref="schema:override",
            schema_version="1.0.0",
            content=42,
        )
    err = json.loads(str(excinfo.value))["error"]
    assert err["code"] == "validation_failed"


@pytest.mark.anyio
async def test_create_record_validation_truncation(configured_server, monkeypatch):
    """When validate_record_content returns a truncated list, create_record reports truncation."""
    from mcp.server.fastmcp.exceptions import ToolError

    from mcp_awareness.tools import create_record, register_schema

    await register_schema(
        source="test",
        tags=[],
        description="s",
        family="schema:trunc",
        version="1.0.0",
        schema={"type": "object"},
    )

    # Patch validate_record_content at the module level so the lazy local import picks it up
    import mcp_awareness.validation as validation_mod

    fake_errors = [
        {"path": f"$.f{i}", "message": "err", "validator": "type", "schema_path": "/type"}
        for i in range(50)
    ] + [{"truncated": True, "total_errors": 99}]

    monkeypatch.setattr(validation_mod, "validate_record_content", lambda _s, _c: fake_errors)

    with pytest.raises(ToolError) as excinfo:
        await create_record(
            source="test",
            tags=[],
            description="truncated errors test",
            logical_key="trunc-rec",
            schema_ref="schema:trunc",
            schema_version="1.0.0",
            content={},
        )
    err = json.loads(str(excinfo.value))["error"]
    assert err["code"] == "validation_failed"
    assert err["truncated"] is True
    assert err["total_errors"] == 99


@pytest.mark.anyio
async def test_update_entry_record_schema_gone(configured_server, store):
    """update_entry re-validation returns schema_not_found if schema was deleted."""
    from mcp.server.fastmcp.exceptions import ToolError

    from mcp_awareness.tools import create_record, register_schema, update_entry

    schema_resp = json.loads(
        await register_schema(
            source="test",
            tags=[],
            description="s",
            family="schema:gone",
            version="1.0.0",
            schema={"type": "object", "properties": {"name": {"type": "string"}}},
        )
    )
    record_resp = json.loads(
        await create_record(
            source="test",
            tags=[],
            description="r",
            logical_key="r-gone",
            schema_ref="schema:gone",
            schema_version="1.0.0",
            content={"name": "ok"},
        )
    )

    # Soft-delete the schema directly via store (bypasses the referencing-records guard)
    store.soft_delete_by_id(TEST_OWNER, schema_resp["id"])

    # Now updating the record's content should fail with schema_not_found
    with pytest.raises(ToolError) as excinfo:
        await update_entry(entry_id=record_resp["id"], content={"name": "updated"})
    err = json.loads(str(excinfo.value))["error"]
    assert err["code"] == "schema_not_found"


@pytest.mark.anyio
async def test_update_entry_record_revalidation_truncation(configured_server, monkeypatch):
    """update_entry re-validation reports truncation when many errors are returned."""
    from mcp.server.fastmcp.exceptions import ToolError

    from mcp_awareness.tools import create_record, register_schema, update_entry

    await register_schema(
        source="test",
        tags=[],
        description="s",
        family="schema:trunc2",
        version="1.0.0",
        schema={"type": "object"},
    )
    record_resp = json.loads(
        await create_record(
            source="test",
            tags=[],
            description="r",
            logical_key="r-trunc2",
            schema_ref="schema:trunc2",
            schema_version="1.0.0",
            content={},
        )
    )

    # Patch validate_record_content at the module level so the lazy local import picks it up
    import mcp_awareness.validation as validation_mod

    fake_errors = [
        {"path": f"$.f{i}", "message": "err", "validator": "type", "schema_path": "/type"}
        for i in range(50)
    ] + [{"truncated": True, "total_errors": 77}]

    monkeypatch.setattr(validation_mod, "validate_record_content", lambda _s, _c: fake_errors)

    with pytest.raises(ToolError) as excinfo:
        await update_entry(entry_id=record_resp["id"], content={"bad": "data"})
    err = json.loads(str(excinfo.value))["error"]
    assert err["code"] == "validation_failed"
    assert err["truncated"] is True
    assert err["total_errors"] == 77
