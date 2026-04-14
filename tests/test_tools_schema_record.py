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

from mcp_awareness.schema import EntryType  # noqa: F401

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
