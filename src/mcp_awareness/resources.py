"""MCP resource handlers for the awareness service.

All ``@mcp.resource`` registrations live here.  The module is imported by
``server.py`` **after** the ``mcp`` instance is created, so the decorators
bind to the live FastMCP object.

Mutable state (``store``, ``mcp``) is accessed via ``_srv.<name>`` so that
test monkeypatching on ``server_mod`` is visible at call time.
"""

from __future__ import annotations

import json

from . import server as _srv
from .collator import generate_briefing
from .helpers import _timed

# ---------------------------------------------------------------------------
# Resources (for MCP clients that support resource reading)
# ---------------------------------------------------------------------------


@_srv.mcp.resource("awareness://briefing")
@_timed
async def briefing_resource() -> str:
    """Compact awareness briefing — the ONLY resource to read at conversation start.
    ~200 tokens when all clear, ~500 when issues exist. Pre-filtered through
    patterns and suppressions. If attention_needed is false, nothing to mention.
    If true, use suggested_mention or compose from source headlines.
    Drill into other resources only if briefing indicates issues or user asks."""
    return json.dumps(generate_briefing(_srv.store), indent=2)


@_srv.mcp.resource("awareness://alerts")
@_timed
async def alerts_resource() -> str:
    """Active alerts across all monitored systems. Empty = all clear.
    This is a drill-down resource — read awareness://briefing first.
    If non-empty, briefly inform user before responding to their question.
    One sentence for warnings, short paragraph for critical.
    Group by source if multiple systems have issues."""
    alerts = _srv.store.get_active_alerts()
    return json.dumps([a.to_dict() for a in alerts], indent=2)


@_srv.mcp.resource("awareness://alerts/{source}")
@_timed
async def source_alerts_resource(source: str) -> str:
    """Active alerts from a specific source. Drill-down from briefing.
    Read this when the briefing references a drill_down for this source."""
    alerts = _srv.store.get_active_alerts(source)
    return json.dumps([a.to_dict() for a in alerts], indent=2)


@_srv.mcp.resource("awareness://status/{source}")
@_timed
async def source_status_resource(source: str) -> str:
    """Full status from a specific source including metrics and inventory.
    Drill-down resource — read when briefing indicates issues with this source
    or when user asks about a specific system."""
    entry = _srv.store.get_latest_status(source)
    if entry:
        return json.dumps(entry.to_dict(), indent=2)
    return json.dumps({"error": f"No status found for source: {source}"})


@_srv.mcp.resource("awareness://knowledge")
@_timed
async def knowledge_resource() -> str:
    """All knowledge entries: learned patterns, historical context, preferences.
    Knowledge belongs to the system, not any specific agent.
    Drill-down resource — read when you need context about a system's
    normal behavior or operational patterns."""
    entries = _srv.store.get_knowledge()
    return json.dumps([e.to_dict() for e in entries], indent=2)


@_srv.mcp.resource("awareness://suppressions")
@_timed
async def suppressions_resource() -> str:
    """Active alert suppressions with expiry times and escalation settings.
    Drill-down resource — the briefing already applies suppressions.
    Read this to show the user what's currently being suppressed."""
    entries = _srv.store.get_active_suppressions()
    return json.dumps([e.to_dict() for e in entries], indent=2)
