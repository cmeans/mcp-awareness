"""FastMCP server — resources + tools for the awareness service.

Transport is selected via the AWARENESS_TRANSPORT environment variable:
  - "stdio" (default): stdin/stdout, for direct MCP client integration
  - "streamable-http": HTTP server on AWARENESS_HOST:AWARENESS_PORT/mcp
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from mcp.server.fastmcp import FastMCP

from .collator import generate_briefing
from .schema import Entry, EntryType, make_id, now_iso
from .store import SQLiteStore, Store

DATA_DIR = os.environ.get("AWARENESS_DATA_DIR", "./data")
TRANSPORT: Literal["stdio", "streamable-http"] = os.environ.get(  # type: ignore[assignment]
    "AWARENESS_TRANSPORT", "stdio"
)
HOST = os.environ.get("AWARENESS_HOST", "0.0.0.0")
PORT = int(os.environ.get("AWARENESS_PORT", "8420"))
MOUNT_PATH = os.environ.get("AWARENESS_MOUNT_PATH", "")

store: Store = SQLiteStore(os.path.join(DATA_DIR, "awareness.db"))

mcp = FastMCP(
    name="mcp-awareness",
    host=HOST,
    port=PORT,
    instructions=(
        "This server provides ambient awareness across monitored systems. "
        "At conversation start, read awareness://briefing. If attention_needed "
        "is true, mention the suggested_mention or compose your own from the "
        "source headlines. If the user asks for details, drill into the "
        "referenced resources. Don't read anything else unless asked or unless "
        "the briefing indicates an issue. Group alerts by source if multiple "
        "systems have issues. One sentence for warnings, short paragraph for "
        "critical. Don't re-check unless asked. When you learn something about "
        "a system from conversation, use learn_pattern to record it. When the "
        "user asks to suppress alerts, use suppress_alert — not a memory edit."
    ),
)


# ---------------------------------------------------------------------------
# Resources (for MCP clients that support resource reading)
# ---------------------------------------------------------------------------


@mcp.resource("awareness://briefing")
async def briefing_resource() -> str:
    """Compact awareness briefing — the ONLY resource to read at conversation start.
    ~200 tokens when all clear, ~500 when issues exist. Pre-filtered through
    patterns and suppressions. If attention_needed is false, nothing to mention.
    If true, use suggested_mention or compose from source headlines.
    Drill into other resources only if briefing indicates issues or user asks."""
    return json.dumps(generate_briefing(store), indent=2)


@mcp.resource("awareness://alerts")
async def alerts_resource() -> str:
    """Active alerts across all monitored systems. Empty = all clear.
    This is a drill-down resource — read awareness://briefing first.
    If non-empty, briefly inform user before responding to their question.
    One sentence for warnings, short paragraph for critical.
    Group by source if multiple systems have issues."""
    alerts = store.get_active_alerts()
    return json.dumps([a.to_dict() for a in alerts], indent=2)


@mcp.resource("awareness://alerts/{source}")
async def source_alerts_resource(source: str) -> str:
    """Active alerts from a specific source. Drill-down from briefing.
    Read this when the briefing references a drill_down for this source."""
    alerts = store.get_active_alerts(source)
    return json.dumps([a.to_dict() for a in alerts], indent=2)


@mcp.resource("awareness://status/{source}")
async def source_status_resource(source: str) -> str:
    """Full status from a specific source including metrics and inventory.
    Drill-down resource — read when briefing indicates issues with this source
    or when user asks about a specific system."""
    entry = store.get_latest_status(source)
    if entry:
        return json.dumps(entry.to_dict(), indent=2)
    return json.dumps({"error": f"No status found for source: {source}"})


@mcp.resource("awareness://knowledge")
async def knowledge_resource() -> str:
    """All knowledge entries: learned patterns, historical context, preferences.
    Knowledge belongs to the system, not any specific agent.
    Drill-down resource — read when you need context about a system's
    normal behavior or operational patterns."""
    entries = store.get_knowledge()
    return json.dumps([e.to_dict() for e in entries], indent=2)


@mcp.resource("awareness://suppressions")
async def suppressions_resource() -> str:
    """Active alert suppressions with expiry times and escalation settings.
    Drill-down resource — the briefing already applies suppressions.
    Read this to show the user what's currently being suppressed."""
    entries = store.get_active_suppressions()
    return json.dumps([e.to_dict() for e in entries], indent=2)


# ---------------------------------------------------------------------------
# Read tools (mirrors of resources, for MCP clients that only support tools)
# ---------------------------------------------------------------------------


@mcp.tool()
async def get_briefing() -> str:
    """Get the awareness briefing. Call this at conversation start.
    Returns a compact summary (~200 tokens all-clear, ~500 with issues).
    If attention_needed is true, mention the suggested_mention or compose
    your own from the source headlines. If false, nothing to report.
    Pre-filtered through patterns and suppressions — no further processing needed."""
    return json.dumps(generate_briefing(store), indent=2)


@mcp.tool()
async def get_alerts(source: str | None = None) -> str:
    """Get active alerts, optionally filtered by source.
    Drill-down from briefing — call when briefing shows attention_needed
    and you want alert details. Returns full alert entries with diagnostics."""
    alerts = store.get_active_alerts(source)
    return json.dumps([a.to_dict() for a in alerts], indent=2)


@mcp.tool()
async def get_status(source: str) -> str:
    """Get full status for a specific source including metrics and inventory.
    Call when the briefing indicates issues with a source or user asks
    about a specific system."""
    entry = store.get_latest_status(source)
    if entry:
        return json.dumps(entry.to_dict(), indent=2)
    return json.dumps({"error": f"No status found for source: {source}"})


@mcp.tool()
async def get_knowledge() -> str:
    """Get all knowledge entries: learned patterns, historical context, preferences.
    Knowledge belongs to the system, not any specific agent. Call when you need
    context about a system's normal behavior or operational patterns."""
    entries = store.get_knowledge()
    return json.dumps([e.to_dict() for e in entries], indent=2)


@mcp.tool()
async def get_suppressions() -> str:
    """Get active alert suppressions with expiry times and escalation settings.
    The briefing already applies suppressions — call this to show the user
    what's currently being suppressed."""
    entries = store.get_active_suppressions()
    return json.dumps([e.to_dict() for e in entries], indent=2)


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@mcp.tool()
async def report_status(
    source: str,
    tags: list[str],
    metrics: dict[str, Any],
    inventory: dict[str, Any] | None = None,
    ttl_sec: int = 120,
) -> str:
    """Report current system status. Called periodically by edge processes.
    If TTL expires without refresh, the source is marked stale in the briefing.
    Each source has one active status entry — new reports replace the previous one."""
    data: dict[str, Any] = {"metrics": metrics, "ttl_sec": ttl_sec}
    if inventory:
        data["inventory"] = inventory
    entry = store.upsert_status(source, tags, data)
    return json.dumps({"status": "ok", "id": entry.id, "source": source})


@mcp.tool()
async def report_alert(
    source: str,
    tags: list[str],
    alert_id: str,
    level: str,
    alert_type: str,
    message: str,
    details: dict[str, Any] | None = None,
    diagnostics: dict[str, Any] | None = None,
    resolved: bool = False,
) -> str:
    """Report an alert or resolve an existing one. Diagnostics should be
    captured at detection time — evidence may be transient. Use resolved=True
    to mark an existing alert as resolved. Alert levels: 'warning', 'critical'.
    Alert types: 'threshold', 'structural', 'baseline'."""
    data: dict[str, Any] = {
        "alert_id": alert_id,
        "level": level,
        "alert_type": alert_type,
        "message": message,
        "resolved": resolved,
    }
    if details:
        data["details"] = details
    if diagnostics:
        data["diagnostics"] = diagnostics
    entry = store.upsert_alert(source, tags, alert_id, data)
    action = "resolved" if resolved else "reported"
    return json.dumps({"status": "ok", "id": entry.id, "action": action, "alert_id": alert_id})


@mcp.tool()
async def learn_pattern(
    source: str,
    tags: list[str],
    description: str,
    conditions: dict[str, Any] | None = None,
    effect: str | None = None,
    learned_from: str = "conversation",
) -> str:
    """Record an operational pattern learned from conversation.
    Any agent can write; any agent can read. Knowledge is portable across platforms.
    Use this when you learn something about a system's normal behavior —
    e.g., 'qBittorrent sometimes stopped for maintenance on Fridays'.
    Do NOT use agent memory for this — use this tool so all agents benefit."""
    now = now_iso()
    entry = Entry(
        id=make_id(),
        type=EntryType.PATTERN,
        source=source,
        tags=tags,
        created=now,
        updated=now,
        expires=None,
        data={
            "description": description,
            "conditions": conditions or {},
            "effect": effect or "",
            "learned_from": learned_from,
        },
    )
    store.add(entry)
    return json.dumps({"status": "ok", "id": entry.id, "description": description})


@mcp.tool()
async def suppress_alert(
    source: str | None = None,
    tags: list[str] | None = None,
    metric: str | None = None,
    level: str = "warning",
    duration_minutes: int = 60,
    escalation_override: bool = True,
    reason: str = "",
) -> str:
    """Suppress alerts. Structured, time-limited, with escalation override.
    Not a plain-text memory edit — survives across agent platforms.
    Use this when the user says things like 'stop bugging me about disk I/O'.
    Escalation override means critical alerts will still break through."""
    now = datetime.now(timezone.utc)
    now_str = now.isoformat()
    expires = (now + timedelta(minutes=duration_minutes)).isoformat()
    entry = Entry(
        id=make_id(),
        type=EntryType.SUPPRESSION,
        source=source or "",
        tags=tags or [],
        created=now_str,
        updated=now_str,
        expires=expires,
        data={
            "metric": metric,
            "suppress_level": level,
            "escalation_override": escalation_override,
            "reason": reason,
            "tags": tags,
        },
    )
    store.add(entry)
    return json.dumps({"status": "ok", "id": entry.id, "expires": expires})


@mcp.tool()
async def add_context(
    source: str,
    tags: list[str],
    description: str,
    expires_days: int = 30,
) -> str:
    """Record historical context that any agent should know about.
    Auto-expires after specified duration. Use this for events like
    'sdb was replaced, RAID rebuilt March 15' — context that's relevant
    for a limited time. Any agent on any platform can read this."""
    now = datetime.now(timezone.utc)
    now_str = now.isoformat()
    expires = (now + timedelta(days=expires_days)).isoformat()
    entry = Entry(
        id=make_id(),
        type=EntryType.CONTEXT,
        source=source,
        tags=tags,
        created=now_str,
        updated=now_str,
        expires=expires,
        data={"description": description},
    )
    store.add(entry)
    return json.dumps({"status": "ok", "id": entry.id, "expires": expires})


@mcp.tool()
async def set_preference(
    key: str,
    value: str,
    scope: str = "global",
) -> str:
    """Set a presentation preference. Portable across agent platforms.
    Use this for preferences like alert_verbosity='one_sentence_warnings'
    or check_frequency='first_turn_only'. These are portable —
    any agent on any platform reads the same preferences."""
    store.upsert_preference(
        key=key,
        scope=scope,
        tags=[],
        data={"key": key, "value": value, "scope": scope},
    )
    return json.dumps({"status": "ok", "key": key, "value": value, "scope": scope})


@mcp.tool()
async def delete_entry(
    source: str | None = None,
    entry_type: str | None = None,
    entry_id: str | None = None,
    confirm: bool = False,
) -> str:
    """Soft-delete entries (moves to trash, recoverable for 30 days). Three modes:
    - By entry_id: trash a single specific entry (no confirm needed).
    - By source + entry_type: trash all entries of that type for the source.
    - By source alone: trash ALL entries for that source.
    For bulk deletes (by source), set confirm=True. Without it, a dry-run count
    is returned so the user can verify before committing.
    Use when the user says 'forget that', 'delete the pattern about X',
    or 'remove everything about Y'. Entries auto-purge after 30 days."""
    if entry_id:
        trashed = store.soft_delete_by_id(entry_id)
        return json.dumps(
            {
                "status": "ok",
                "trashed": 1 if trashed else 0,
                "entry_id": entry_id,
                "recoverable_days": 30,
            }
        )
    if not source:
        return json.dumps({"status": "error", "message": "Provide entry_id or source"})
    et = EntryType(entry_type) if entry_type else None
    if not confirm:
        entries = store.get_entries(entry_type=et, source=source)
        return json.dumps(
            {
                "status": "dry_run",
                "would_trash": len(entries),
                "source": source,
                "entry_type": entry_type,
                "message": "Set confirm=True to move to trash. Show the user this count first.",
            }
        )
    count = store.soft_delete_by_source(source, et)
    return json.dumps(
        {
            "status": "ok",
            "trashed": count,
            "source": source,
            "entry_type": entry_type,
            "recoverable_days": 30,
        }
    )


@mcp.tool()
async def restore_entry(entry_id: str) -> str:
    """Restore a soft-deleted entry from the trash. Requires the entry ID.
    Call get_deleted first to see what's in the trash and get the IDs."""
    restored = store.restore_by_id(entry_id)
    return json.dumps(
        {
            "status": "ok" if restored else "not_found",
            "restored": restored,
            "entry_id": entry_id,
        }
    )


@mcp.tool()
async def get_deleted() -> str:
    """List all entries in the trash (soft-deleted, recoverable).
    Returns entries with their IDs so they can be restored via restore_entry.
    Trashed entries auto-purge after 30 days."""
    entries = store.get_deleted()
    return json.dumps([e.to_dict() for e in entries], indent=2)


def main() -> None:
    if TRANSPORT == "streamable-http" and MOUNT_PATH:
        import uvicorn
        from starlette.types import ASGIApp, Receive, Scope, Send

        inner_app = mcp.streamable_http_app()

        class SecretPathMiddleware:
            """Rewrite /SECRET/mcp → /mcp, reject everything else."""

            def __init__(self, app: ASGIApp, prefix: str) -> None:
                self.app = app
                self.prefix = prefix.rstrip("/")

            async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
                if scope["type"] in ("http", "websocket"):
                    path: str = scope.get("path", "")
                    if path.startswith(self.prefix):
                        scope = dict(scope)
                        scope["path"] = path[len(self.prefix) :] or "/"
                        await self.app(scope, receive, send)
                        return
                    # Not the secret path — 404
                    from starlette.responses import Response

                    response = Response("Not Found", status_code=404)
                    await response(scope, receive, send)
                    return
                await self.app(scope, receive, send)

        app = SecretPathMiddleware(inner_app, MOUNT_PATH)

        config = uvicorn.Config(app, host=HOST, port=PORT)
        server = uvicorn.Server(config)

        import anyio

        anyio.run(server.serve)
    else:
        mcp.run(transport=TRANSPORT)


if __name__ == "__main__":
    main()
