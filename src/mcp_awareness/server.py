"""FastMCP server — resources + tools for the awareness service.

Transport is selected via the AWARENESS_TRANSPORT environment variable:
  - "stdio" (default): stdin/stdout, for direct MCP client integration
  - "streamable-http": HTTP server on AWARENESS_HOST:AWARENESS_PORT/mcp
"""

from __future__ import annotations

import functools
import json
import os
import time
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from mcp.server.fastmcp import FastMCP

from .collator import generate_briefing
from .schema import Entry, EntryType, make_id, now_iso
from .store import SQLiteStore, Store

_start_time = time.monotonic()

DATA_DIR = os.environ.get("AWARENESS_DATA_DIR", "./data")
TRANSPORT: Literal["stdio", "streamable-http"] = os.environ.get(  # type: ignore[assignment]
    "AWARENESS_TRANSPORT", "stdio"
)
HOST = os.environ.get("AWARENESS_HOST", "0.0.0.0")
PORT = int(os.environ.get("AWARENESS_PORT", "8420"))
MOUNT_PATH = os.environ.get("AWARENESS_MOUNT_PATH", "")
BACKEND = os.environ.get("AWARENESS_BACKEND", "sqlite")
DATABASE_URL = os.environ.get("AWARENESS_DATABASE_URL", "")


def _create_store() -> Store:
    """Create the storage backend based on AWARENESS_BACKEND env var."""
    if BACKEND == "postgres":
        if not DATABASE_URL:
            raise ValueError(
                "AWARENESS_DATABASE_URL is required when AWARENESS_BACKEND=postgres. "
                "Example: postgresql://user:pass@localhost:5432/awareness"
            )
        from .postgres_store import PostgresStore

        return PostgresStore(DATABASE_URL)
    return SQLiteStore(os.path.join(DATA_DIR, "awareness.db"))


store: Store = _create_store()


def _log_timing(tool_name: str, elapsed_ms: float) -> None:
    """Log tool call timing to stdout (Docker captures automatically)."""
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    print(f"{ts} | {tool_name} | {elapsed_ms:.1f}ms", flush=True)


def _timed(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Decorator that logs wall-clock time for each tool/resource call."""

    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        t0 = time.monotonic()
        result = await fn(*args, **kwargs)
        _log_timing(fn.__name__, (time.monotonic() - t0) * 1000)
        return result

    return wrapper


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
@_timed
async def briefing_resource() -> str:
    """Compact awareness briefing — the ONLY resource to read at conversation start.
    ~200 tokens when all clear, ~500 when issues exist. Pre-filtered through
    patterns and suppressions. If attention_needed is false, nothing to mention.
    If true, use suggested_mention or compose from source headlines.
    Drill into other resources only if briefing indicates issues or user asks."""
    return json.dumps(generate_briefing(store), indent=2)


@mcp.resource("awareness://alerts")
@_timed
async def alerts_resource() -> str:
    """Active alerts across all monitored systems. Empty = all clear.
    This is a drill-down resource — read awareness://briefing first.
    If non-empty, briefly inform user before responding to their question.
    One sentence for warnings, short paragraph for critical.
    Group by source if multiple systems have issues."""
    alerts = store.get_active_alerts()
    return json.dumps([a.to_dict() for a in alerts], indent=2)


@mcp.resource("awareness://alerts/{source}")
@_timed
async def source_alerts_resource(source: str) -> str:
    """Active alerts from a specific source. Drill-down from briefing.
    Read this when the briefing references a drill_down for this source."""
    alerts = store.get_active_alerts(source)
    return json.dumps([a.to_dict() for a in alerts], indent=2)


@mcp.resource("awareness://status/{source}")
@_timed
async def source_status_resource(source: str) -> str:
    """Full status from a specific source including metrics and inventory.
    Drill-down resource — read when briefing indicates issues with this source
    or when user asks about a specific system."""
    entry = store.get_latest_status(source)
    if entry:
        return json.dumps(entry.to_dict(), indent=2)
    return json.dumps({"error": f"No status found for source: {source}"})


@mcp.resource("awareness://knowledge")
@_timed
async def knowledge_resource() -> str:
    """All knowledge entries: learned patterns, historical context, preferences.
    Knowledge belongs to the system, not any specific agent.
    Drill-down resource — read when you need context about a system's
    normal behavior or operational patterns."""
    entries = store.get_knowledge()
    return json.dumps([e.to_dict() for e in entries], indent=2)


@mcp.resource("awareness://suppressions")
@_timed
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
@_timed
async def get_briefing() -> str:
    """Get the awareness briefing. Call this at conversation start.
    Returns a compact summary (~200 tokens all-clear, ~500 with issues).
    If attention_needed is true, mention the suggested_mention or compose
    your own from the source headlines. If false, nothing to report.
    Pre-filtered through patterns and suppressions — no further processing needed.
    This tool always returns structured JSON. If you receive an unstructured
    error, the failure is in the transport or platform layer, not in awareness."""
    return json.dumps(generate_briefing(store), indent=2)


@mcp.tool()
@_timed
async def get_alerts(source: str | None = None) -> str:
    """Get active alerts, optionally filtered by source.
    Drill-down from briefing — call when briefing shows attention_needed
    and you want alert details. Returns full alert entries with diagnostics.
    This tool always returns structured JSON. If you receive an unstructured
    error, the failure is in the transport or platform layer, not in awareness."""
    alerts = store.get_active_alerts(source)
    return json.dumps([a.to_dict() for a in alerts], indent=2)


@mcp.tool()
@_timed
async def get_status(source: str) -> str:
    """Get full status for a specific source including metrics and inventory.
    Call when the briefing indicates issues with a source or user asks
    about a specific system. This tool always returns structured JSON.
    If you receive an unstructured error, the failure is in the transport
    or platform layer, not in awareness."""
    entry = store.get_latest_status(source)
    if entry:
        return json.dumps(entry.to_dict(), indent=2)
    return json.dumps({"error": f"No status found for source: {source}"})


@mcp.tool()
@_timed
async def get_knowledge(
    source: str | None = None,
    tags: list[str] | None = None,
    entry_type: str | None = None,
    include_history: str | None = None,
) -> str:
    """Get knowledge entries: learned patterns, historical context, preferences, notes.
    Knowledge belongs to the system, not any specific agent. Call when you need
    context about a system's normal behavior, operational patterns, or stored notes.
    Filter by source, tags, and/or entry_type to reduce response size.
    Valid entry_type values: 'pattern', 'context', 'preference', 'note'.
    include_history: omit or 'false' to strip change history, 'true' to include,
    'only' to return only entries with change history.
    This tool always returns JSON with a status field or an entry list.
    If you receive an unstructured error, the failure is in the transport
    or platform layer, not in awareness."""
    if entry_type:
        et = EntryType(entry_type)
        entries = store.get_entries(entry_type=et, source=source, tags=tags)
    else:
        entries = store.get_knowledge(tags=tags, include_history=include_history)
        if source:
            entries = [e for e in entries if e.source == source]
    return json.dumps([e.to_dict() for e in entries], indent=2)


@mcp.tool()
@_timed
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
@_timed
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
@_timed
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
@_timed
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
    Do NOT use agent memory for this — use this tool so all agents benefit.
    Returns JSON with status and entry id. If you receive an unstructured
    error, the failure is in the transport or platform layer, not in awareness."""
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
@_timed
async def remember(
    source: str,
    tags: list[str],
    description: str,
    content: str | None = None,
    content_type: str = "text/plain",
    learned_from: str = "conversation",
) -> str:
    """Store a general-purpose note. Use this for any knowledge that doesn't fit
    operational patterns (learn_pattern) or time-limited context (add_context).
    Examples: personal facts, project notes, skill backups, config snapshots.
    description is a short summary; content is the optional payload (text, JSON, etc.).
    content_type is a MIME type (default text/plain). Set learned_from to your platform.
    Returns JSON with status and entry id. If you receive an unstructured
    error, the failure is in the transport or platform layer, not in awareness."""
    now = now_iso()
    data: dict[str, Any] = {
        "description": description,
        "learned_from": learned_from,
    }
    if content is not None:
        data["content"] = content
        data["content_type"] = content_type
    entry = Entry(
        id=make_id(),
        type=EntryType.NOTE,
        source=source,
        tags=tags,
        created=now,
        updated=now,
        expires=None,
        data=data,
    )
    store.add(entry)
    return json.dumps({"status": "ok", "id": entry.id, "description": description})


@mcp.tool()
@_timed
async def update_entry(
    entry_id: str,
    description: str | None = None,
    tags: list[str] | None = None,
    source: str | None = None,
    content: str | None = None,
    content_type: str | None = None,
) -> str:
    """Update an existing entry in place, preserving its ID and creation timestamp.
    Only works on knowledge types: note, pattern, context, preference.
    Status, alert, and suppression entries are immutable.
    Only provided fields are updated — omit fields to leave them unchanged.
    Changes are tracked in a changelog array within the entry data.
    Use get_knowledge(include_history='true') to see change history.
    Returns JSON with status. If you receive an unstructured error, the failure
    is in the transport or platform layer, not in awareness."""
    updates: dict[str, Any] = {}
    if description is not None:
        updates["description"] = description
    if tags is not None:
        updates["tags"] = tags
    if source is not None:
        updates["source"] = source
    if content is not None:
        updates["content"] = content
    if content_type is not None:
        updates["content_type"] = content_type
    if not updates:
        return json.dumps({"status": "error", "message": "No fields to update"})
    result = store.update_entry(entry_id, updates)
    if result is None:
        return json.dumps(
            {
                "status": "error",
                "message": "Entry not found or type is immutable (status/alert/suppression)",
            }
        )
    return json.dumps({"status": "ok", "id": result.id, "updated": result.updated})


@mcp.tool()
@_timed
async def get_stats() -> str:
    """Get summary statistics: entry counts by type, list of sources, total count.
    Call before get_knowledge to decide whether to pull everything or filter.
    This tool always returns structured JSON. If you receive an unstructured
    error, the failure is in the transport or platform layer, not in awareness."""
    return json.dumps(store.get_stats(), indent=2)


@mcp.tool()
@_timed
async def get_tags() -> str:
    """Get all tags in use with usage counts, sorted by count descending.
    Use this to discover existing tags before creating new ones — prevents
    tag drift (e.g., 'infrastructure' vs 'infra'). This tool always returns
    structured JSON. If you receive an unstructured error, the failure is in
    the transport or platform layer, not in awareness."""
    return json.dumps(store.get_tags(), indent=2)


@mcp.tool()
@_timed
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
        },
    )
    store.add(entry)
    return json.dumps({"status": "ok", "id": entry.id, "expires": expires})


@mcp.tool()
@_timed
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
@_timed
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
@_timed
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
    or 'remove everything about Y'. Entries auto-purge after 30 days.
    Returns JSON with status and count. If you receive an unstructured
    error, the failure is in the transport or platform layer, not in awareness."""
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
@_timed
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
@_timed
async def get_deleted() -> str:
    """List all entries in the trash (soft-deleted, recoverable).
    Returns entries with their IDs so they can be restored via restore_entry.
    Trashed entries auto-purge after 30 days."""
    entries = store.get_deleted()
    return json.dumps([e.to_dict() for e in entries], indent=2)


def _health_response() -> dict[str, Any]:
    """Build the health check response payload."""
    return {
        "status": "ok",
        "uptime_sec": round(time.monotonic() - _start_time, 1),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "transport": TRANSPORT,
    }


def main() -> None:
    if TRANSPORT == "streamable-http" and MOUNT_PATH:
        import uvicorn
        from starlette.responses import JSONResponse, Response
        from starlette.types import ASGIApp, Receive, Scope, Send

        inner_app = mcp.streamable_http_app()

        class SecretPathMiddleware:
            """Rewrite /SECRET/mcp → /mcp, serve /SECRET/health, reject everything else."""

            def __init__(self, app: ASGIApp, prefix: str) -> None:
                self.app = app
                self.prefix = prefix.rstrip("/")

            async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
                if scope["type"] in ("http", "websocket"):
                    path: str = scope.get("path", "")
                    # Health endpoint — served at /SECRET/health
                    if path == f"{self.prefix}/health":
                        health_resp = JSONResponse(_health_response())
                        await health_resp(scope, receive, send)
                        return
                    if path.startswith(self.prefix):
                        scope = dict(scope)
                        scope["path"] = path[len(self.prefix) :] or "/"
                        await self.app(scope, receive, send)
                        return
                    # Not the secret path — 404
                    not_found = Response("Not Found", status_code=404)
                    await not_found(scope, receive, send)
                    return
                await self.app(scope, receive, send)

        app = SecretPathMiddleware(inner_app, MOUNT_PATH)

        config = uvicorn.Config(app, host=HOST, port=PORT)
        server = uvicorn.Server(config)

        import anyio

        anyio.run(server.serve)
    elif TRANSPORT == "streamable-http":
        import uvicorn
        from starlette.applications import Starlette
        from starlette.responses import JSONResponse
        from starlette.routing import Route

        mcp_app = mcp.streamable_http_app()

        async def health_endpoint(request: Any) -> JSONResponse:
            return JSONResponse(_health_response())

        http_app = Starlette(
            routes=[Route("/health", health_endpoint)],
            on_startup=[],
        )
        http_app.mount("/", mcp_app)

        config = uvicorn.Config(http_app, host=HOST, port=PORT)
        server = uvicorn.Server(config)

        import anyio

        anyio.run(server.serve)
    else:
        mcp.run(transport=TRANSPORT)


if __name__ == "__main__":
    main()
