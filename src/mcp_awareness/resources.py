"""MCP resource handlers for the awareness service.

Resource functions are defined at module level (using helpers.store) so they
can be imported by tests.  register_resources() wires them into the FastMCP
instance.
"""

from __future__ import annotations

import json
from typing import Any

from mcp.server.fastmcp import FastMCP

from .collator import generate_briefing
from .helpers import _timed, store

# ---------------------------------------------------------------------------
# Resource handler functions
# ---------------------------------------------------------------------------


@_timed
async def briefing_resource() -> str:
    """Compact awareness briefing — the ONLY resource to read at conversation start.
    ~200 tokens when all clear, ~500 when issues exist. Pre-filtered through
    patterns and suppressions. If attention_needed is false, nothing to mention.
    If true, use suggested_mention or compose from source headlines.
    Drill into other resources only if briefing indicates issues or user asks."""
    return json.dumps(generate_briefing(store), indent=2)


@_timed
async def alerts_resource() -> str:
    """Active alerts across all monitored systems. Empty = all clear.
    This is a drill-down resource — read awareness://briefing first.
    If non-empty, briefly inform user before responding to their question.
    One sentence for warnings, short paragraph for critical.
    Group by source if multiple systems have issues."""
    alerts = store.get_active_alerts()
    return json.dumps([a.to_dict() for a in alerts], indent=2)


@_timed
async def source_alerts_resource(source: str) -> str:
    """Active alerts from a specific source. Drill-down from briefing.
    Read this when the briefing references a drill_down for this source."""
    alerts = store.get_active_alerts(source)
    return json.dumps([a.to_dict() for a in alerts], indent=2)


@_timed
async def source_status_resource(source: str) -> str:
    """Full status from a specific source including metrics and inventory.
    Drill-down resource — read when briefing indicates issues with this source
    or when user asks about a specific system."""
    entry = store.get_latest_status(source)
    if entry:
        return json.dumps(entry.to_dict(), indent=2)
    return json.dumps({"error": f"No status found for source: {source}"})


@_timed
async def knowledge_resource() -> str:
    """All knowledge entries: learned patterns, historical context, preferences.
    Knowledge belongs to the system, not any specific agent.
    Drill-down resource — read when you need context about a system's
    normal behavior or operational patterns."""
    entries = store.get_knowledge()
    return json.dumps([e.to_dict() for e in entries], indent=2)


@_timed
async def suppressions_resource() -> str:
    """Active alert suppressions with expiry times and escalation settings.
    Drill-down resource — the briefing already applies suppressions.
    Read this to show the user what's currently being suppressed."""
    entries = store.get_active_suppressions()
    return json.dumps([e.to_dict() for e in entries], indent=2)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def register_resources(mcp: FastMCP, store_ref: Any) -> None:
    """Register all resource handlers on the given FastMCP instance."""
    mcp.resource("awareness://briefing")(briefing_resource)
    mcp.resource("awareness://alerts")(alerts_resource)
    mcp.resource("awareness://alerts/{source}")(source_alerts_resource)
    mcp.resource("awareness://status/{source}")(source_status_resource)
    mcp.resource("awareness://knowledge")(knowledge_resource)
    mcp.resource("awareness://suppressions")(suppressions_resource)
