"""FastMCP server — resources + tools for the awareness service.

Transport is selected via the AWARENESS_TRANSPORT environment variable:
  - "stdio" (default): stdin/stdout, for direct MCP client integration
  - "streamable-http": HTTP server on AWARENESS_HOST:AWARENESS_PORT/mcp
"""

from __future__ import annotations

import functools
import json
import os
import re
import time
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from mcp.server.fastmcp import FastMCP

from .collator import generate_briefing
from .postgres_store import PostgresStore
from .schema import Entry, EntryType, ensure_dt, make_id, now_utc, to_iso
from .store import Store

_start_time = time.monotonic()

TRANSPORT: Literal["stdio", "streamable-http"] = os.environ.get(  # type: ignore[assignment]
    "AWARENESS_TRANSPORT", "stdio"
)
HOST = os.environ.get("AWARENESS_HOST", "0.0.0.0")
PORT = int(os.environ.get("AWARENESS_PORT", "8420"))
MOUNT_PATH = os.environ.get("AWARENESS_MOUNT_PATH", "")
DATABASE_URL = os.environ.get("AWARENESS_DATABASE_URL", "")


def _create_store() -> Store:
    """Create the PostgreSQL storage backend.

    Returns a PostgresStore if DATABASE_URL is set, otherwise raises.
    Called lazily at first use (not at import time) to avoid side effects
    during testing and to allow monkeypatching before initialization.
    """
    url = os.environ.get("AWARENESS_DATABASE_URL", "")
    if not url:
        raise ValueError(
            "AWARENESS_DATABASE_URL is required. "
            "Example: postgresql://user:pass@localhost:5432/awareness"
        )
    return PostgresStore(url)


class _LazyStore:
    """Descriptor that initializes the store on first attribute access.

    Avoids import-time side effects (DB connections, env var requirements).
    Tests can monkeypatch server_mod.store before any access occurs.
    """

    _instance: Store | None = None

    def __getattr__(self, name: str) -> Any:
        if _LazyStore._instance is None:
            _LazyStore._instance = _create_store()
        return getattr(_LazyStore._instance, name)


store: Any = _LazyStore()


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
async def get_alerts(
    source: str | None = None,
    since: str | None = None,
    mode: str | None = None,
    limit: int | None = None,
    offset: int | None = None,
) -> str:
    """Get active alerts, optionally filtered by source.
    Drill-down from briefing — call when briefing shows attention_needed
    and you want alert details. Returns full alert entries with diagnostics.
    since: ISO 8601 timestamp — only return alerts updated after this time.
    mode: omit for full entries, 'list' for metadata only.
    Use limit/offset for pagination (e.g., limit=10, offset=0 for first page).
    This tool always returns structured JSON. If you receive an unstructured
    error, the failure is in the transport or platform layer, not in awareness."""
    since_dt = ensure_dt(since) if since else None
    alerts = store.get_active_alerts(source, since=since_dt, limit=limit, offset=offset)
    if mode == "list":
        return json.dumps([a.to_list_dict() for a in alerts], indent=2)
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
    since: str | None = None,
    mode: str | None = None,
    limit: int | None = None,
    offset: int | None = None,
) -> str:
    """Get knowledge entries: learned patterns, historical context, preferences, notes.
    Knowledge belongs to the system, not any specific agent. Call when you need
    context about a system's normal behavior, operational patterns, or stored notes.
    Filter by source, tags, and/or entry_type to reduce response size.
    Valid entry_type values: 'pattern', 'context', 'preference', 'note'.
    include_history: omit or 'false' to strip change history, 'true' to include,
    'only' to return only entries with change history.
    since: ISO 8601 timestamp — only return entries updated after this time.
    Useful for catching up on recent changes (e.g., since='2026-03-23T06:00:00Z').
    mode: omit for full entries, 'list' for metadata only (id, type, source,
    description, tags, created, updated — no content or changelog). Use 'list'
    to orient before pulling full entries.
    Use limit/offset for pagination (e.g., limit=10, offset=0 for first page).
    This tool always returns JSON with a status field or an entry list.
    If you receive an unstructured error, the failure is in the transport
    or platform layer, not in awareness."""
    since_dt = ensure_dt(since) if since else None
    if entry_type:
        et = EntryType(entry_type)
        entries = store.get_entries(
            entry_type=et, source=source, tags=tags, since=since_dt,
            limit=limit, offset=offset,
        )
    else:
        entries = store.get_knowledge(
            tags=tags, include_history=include_history, since=since_dt,
            limit=limit, offset=offset,
        )
        if source:
            entries = [e for e in entries if e.source == source]
    if mode == "list":
        return json.dumps([e.to_list_dict() for e in entries], indent=2)
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
    now = now_utc()
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
    logical_key: str | None = None,
) -> str:
    """Store a general-purpose note. Use this for any knowledge that doesn't fit
    operational patterns (learn_pattern) or time-limited context (add_context).
    Examples: personal facts, project notes, skill backups, config snapshots.
    description is a short summary; content is the optional payload (text, JSON, etc.).
    content_type is a MIME type (default text/plain). Set learned_from to your platform.
    logical_key is an optional identifier for upsert behavior — if a note with the
    same source + logical_key exists, it will be updated in place (with changelog
    tracking) instead of creating a duplicate. Use for living documents like
    project status notes.
    Returns JSON with status and entry id. If you receive an unstructured
    error, the failure is in the transport or platform layer, not in awareness."""
    now = now_utc()
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
        logical_key=logical_key,
    )
    if logical_key:
        result, created = store.upsert_by_logical_key(source, logical_key, entry)
        action = "created" if created else "updated"
        return json.dumps(
            {"status": "ok", "id": result.id, "action": action, "description": description}
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
    return json.dumps({"status": "ok", "id": result.id, "updated": to_iso(result.updated)})


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
    now = now_utc()
    expires = now + timedelta(minutes=duration_minutes)
    entry = Entry(
        id=make_id(),
        type=EntryType.SUPPRESSION,
        source=source or "",
        tags=tags or [],
        created=now,
        updated=now,
        expires=expires,
        data={
            "metric": metric,
            "suppress_level": level,
            "escalation_override": escalation_override,
            "reason": reason,
        },
    )
    store.add(entry)
    return json.dumps({"status": "ok", "id": entry.id, "expires": to_iso(expires)})


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
    now = now_utc()
    expires = now + timedelta(days=expires_days)
    entry = Entry(
        id=make_id(),
        type=EntryType.CONTEXT,
        source=source,
        tags=tags,
        created=now,
        updated=now,
        expires=expires,
        data={"description": description},
    )
    store.add(entry)
    return json.dumps({"status": "ok", "id": entry.id, "expires": to_iso(expires)})


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
    tags: list[str] | None = None,
    confirm: bool = False,
) -> str:
    """Soft-delete entries (moves to trash, recoverable for 30 days). Four modes:
    - By entry_id: trash a single specific entry (no confirm needed).
    - By tags: trash all entries matching ALL given tags (AND logic). confirm required.
    - By source + entry_type: trash all entries of that type for the source.
    - By source alone: trash ALL entries for that source.
    For bulk deletes, set confirm=True. Without it, a dry-run count
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
    if tags:
        if not confirm:
            entries = store.get_entries(tags=tags)
            return json.dumps(
                {
                    "status": "dry_run",
                    "would_trash": len(entries),
                    "tags": tags,
                    "message": "Set confirm=True to move to trash. Show the user this count first.",
                }
            )
        count = store.soft_delete_by_tags(tags)
        return json.dumps(
            {
                "status": "ok",
                "trashed": count,
                "tags": tags,
                "recoverable_days": 30,
            }
        )
    if not source:
        return json.dumps({"status": "error", "message": "Provide entry_id, tags, or source"})
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
async def restore_entry(
    entry_id: str | None = None,
    tags: list[str] | None = None,
) -> str:
    """Restore soft-deleted entries from the trash. Two modes:
    - By entry_id: restore a single specific entry.
    - By tags: restore all trashed entries matching ALL given tags (AND logic).
    Call get_deleted first to see what's in the trash."""
    if entry_id:
        restored = store.restore_by_id(entry_id)
        return json.dumps(
            {
                "status": "ok" if restored else "not_found",
                "restored": 1 if restored else 0,
                "entry_id": entry_id,
            }
        )
    if tags:
        count = store.restore_by_tags(tags)
        return json.dumps({"status": "ok", "restored": count, "tags": tags})
    return json.dumps({"status": "error", "message": "Provide entry_id or tags"})


@mcp.tool()
@_timed
async def get_deleted(
    since: str | None = None,
    mode: str | None = None,
    limit: int | None = None,
    offset: int | None = None,
) -> str:
    """List all entries in the trash (soft-deleted, recoverable).
    Returns entries with their IDs so they can be restored via restore_entry.
    Trashed entries auto-purge after 30 days.
    since: ISO 8601 timestamp — only return entries deleted after this time.
    mode: omit for full entries, 'list' for metadata only.
    Use limit/offset for pagination."""
    since_dt = ensure_dt(since) if since else None
    entries = store.get_deleted(since=since_dt, limit=limit, offset=offset)
    if mode == "list":
        return json.dumps([e.to_list_dict() for e in entries], indent=2)
    return json.dumps([e.to_dict() for e in entries], indent=2)


# ---------------------------------------------------------------------------
# Prompts (discoverable agent instructions, built from store data)
# ---------------------------------------------------------------------------


def _extract_entry_number(desc: str) -> int:
    """Extract 'Entry N' number from description for sorting."""
    m = re.search(r"Entry\s+(\d+)", desc)
    return int(m.group(1)) if m else 99


@mcp.prompt(
    name="agent_instructions",
    description=(
        "Complete agent instructions for using awareness — reading, writing, "
        "tag conventions, quality rules, status maintenance, and resilience. "
        "Built dynamically from the user's stored conventions. "
        "Call this once at conversation start to learn how to use awareness correctly."
    ),
)
@_timed
async def agent_instructions() -> str:
    """Compose agent instructions from awareness-prompt entries in the store."""
    _sync_custom_prompts()
    entries = store.get_knowledge(tags=["memory-prompt"])
    # Sort by entry number (Entry 1, Entry 2, etc.)
    entries.sort(key=lambda e: _extract_entry_number(e.data.get("description", "")))

    if not entries:
        return (
            "No agent instructions found in the awareness store. "
            "Store entries with source='awareness-prompt' and tags=['memory-prompt'] "
            "to populate this prompt."
        )

    sections = []
    for e in entries:
        desc = e.data.get("description", "")
        # Extract the section name from "Entry N (Name):" pattern
        m = re.match(r"Awareness prompt Entry \d+ \(([^)]+)\):\s*(.*)", desc, re.DOTALL)
        if m:
            sections.append(f"## {m.group(1)}\n{m.group(2).strip()}")
        else:
            sections.append(desc)

    return "# Awareness Agent Instructions\n\n" + "\n\n".join(sections)


@mcp.prompt(
    name="project_context",
    description=(
        "Get everything awareness knows about a project — knowledge entries, "
        "status, active alerts, and relevant patterns. Pass the repo name "
        "(e.g., 'mcp-awareness') to get a composed project briefing."
    ),
)
@_timed
async def project_context(repo_name: str) -> str:
    """Compose a project briefing from all knowledge tagged with repo_name."""
    entries = store.get_knowledge(tags=[repo_name])
    alerts = store.get_active_alerts()
    alerts = [a for a in alerts if repo_name in a.tags]

    parts: list[str] = [f"# Project Context: {repo_name}"]

    if not entries and not alerts:
        parts.append(f"\nNo knowledge or alerts found for '{repo_name}'.")
        return "\n".join(parts)

    if alerts:
        parts.append(f"\n## Active Alerts ({len(alerts)})")
        for a in alerts:
            level = a.data.get("level", "unknown")
            msg = a.data.get("message", "")
            parts.append(f"- **{level}**: {msg}")

    # Group entries by type
    by_type: dict[str, list[Entry]] = {}
    for e in entries:
        type_name = e.type.value if isinstance(e.type, EntryType) else str(e.type)
        by_type.setdefault(type_name, []).append(e)

    for type_name, type_entries in by_type.items():
        parts.append(f"\n## {type_name.title()} ({len(type_entries)})")
        for e in type_entries:
            desc = e.data.get("description", "(no description)")
            # Truncate long descriptions for the overview
            if len(desc) > 200:
                desc = desc[:200] + "..."
            parts.append(f"- [{e.source}] {desc}")

    return "\n".join(parts)


@mcp.prompt(
    name="system_status",
    description=(
        "Get a composed narrative for a specific system — latest status, "
        "active alerts, and relevant patterns. Pass the source name "
        "(e.g., 'synology-nas')."
    ),
)
@_timed
async def system_status(source: str) -> str:
    """Compose a system status narrative from status, alerts, and patterns."""
    status = store.get_latest_status(source)
    alerts = store.get_active_alerts(source=source)
    patterns = store.get_patterns(source=source)

    parts: list[str] = [f"# System Status: {source}"]

    if not status and not alerts:
        parts.append(f"\nNo status or alerts found for '{source}'.")
        return "\n".join(parts)

    if status:
        parts.append("\n## Current Status")
        metrics = status.data.get("metrics", {})
        if metrics:
            for k, v in metrics.items():
                parts.append(f"- {k}: {v}")
        desc = status.data.get("description", "")
        if desc:
            parts.append(f"- {desc}")
        parts.append(f"- Last report: {to_iso(status.updated)}")

    if alerts:
        unresolved = [a for a in alerts if not a.data.get("resolved")]
        parts.append(f"\n## Active Alerts ({len(unresolved)})")
        for a in unresolved:
            level = a.data.get("level", "unknown")
            msg = a.data.get("message", "")
            parts.append(f"- **{level}**: {msg}")

    if patterns:
        parts.append(f"\n## Known Patterns ({len(patterns)})")
        for p in patterns:
            condition = p.data.get("condition", "")
            effect = p.data.get("effect", "")
            parts.append(f"- When: {condition} → {effect}")

    return "\n".join(parts)


@mcp.prompt(
    name="write_guide",
    description=(
        "Interactive guide for writing to awareness — shows existing sources, "
        "tags with usage counts, and entry type distribution. Call before "
        "writing to avoid source/tag drift and duplicates."
    ),
)
@_timed
async def write_guide() -> str:
    """Compose a write guide from current store stats and tags."""
    stats = store.get_stats()
    tags = store.get_tags()

    parts: list[str] = ["# Awareness Write Guide"]

    # Entry counts
    parts.append("\n## Store Overview")
    parts.append(f"Total entries: {stats['total']}")
    for entry_type, count in stats["entries"].items():
        if count > 0:
            parts.append(f"- {entry_type}: {count}")

    # Sources
    parts.append(f"\n## Active Sources ({len(stats['sources'])})")
    parts.append("Use these exact source names — don't create variants:")
    for src in sorted(stats["sources"]):
        parts.append(f"- `{src}`")

    # Tags
    parts.append(f"\n## Tags in Use ({len(tags)})")
    parts.append("Check this list before creating new tags:")
    for t in tags[:30]:  # Cap at 30 to keep it manageable
        parts.append(f"- `{t['tag']}` ({t['count']})")
    if len(tags) > 30:
        parts.append(f"- ... and {len(tags) - 30} more")

    # Quick reference
    parts.append("\n## Quick Reference")
    parts.append("- `remember` — general notes (default for most knowledge)")
    parts.append("- `learn_pattern` — ONLY for condition/effect pairs used by alert collator")
    parts.append("- `add_context` — time-limited context (auto-expires)")
    parts.append("- `set_preference` — behavioral preferences (upserts by key+scope)")
    parts.append("- Use `logical_key` for living documents that should upsert")

    return "\n".join(parts)


@mcp.prompt(
    name="catchup",
    description=(
        "What changed recently? Shows entries updated in the last N hours "
        "(default 24). Use at conversation start to see what other agents "
        "or platforms have written since you were last active."
    ),
)
@_timed
async def catchup(hours: int = 24) -> str:
    """Compose a catchup summary of recently updated entries."""
    since = now_utc() - timedelta(hours=hours)
    # Pull all knowledge and filter by updated timestamp
    all_entries = store.get_knowledge(include_history="true")
    recent = [e for e in all_entries if e.updated >= since]
    # Also check alerts
    alerts = store.get_active_alerts()
    recent_alerts = [a for a in alerts if a.updated >= since]

    parts: list[str] = [f"# Catchup — last {hours} hours"]

    if not recent and not recent_alerts:
        parts.append("\nNothing changed. You're up to date.")
        return "\n".join(parts)

    if recent_alerts:
        parts.append(f"\n## New/Updated Alerts ({len(recent_alerts)})")
        for a in recent_alerts:
            level = a.data.get("level", "unknown")
            msg = a.data.get("message", "")
            parts.append(f"- **{level}** [{a.source}]: {msg}")

    if recent:
        # Group by source
        by_source: dict[str, list[Entry]] = {}
        for e in recent:
            by_source.setdefault(e.source, []).append(e)

        parts.append(f"\n## Updated Knowledge ({len(recent)} entries)")
        for src, entries in sorted(by_source.items()):
            parts.append(f"\n### {src} ({len(entries)})")
            for e in entries:
                desc = e.data.get("description", "(no description)")
                if len(desc) > 150:
                    desc = desc[:150] + "..."
                has_changelog = "changelog" in e.data
                marker = " [updated]" if has_changelog else " [new]"
                parts.append(f"- {desc}{marker}")

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# User-defined prompts (stored as entries with source="custom-prompt")
# ---------------------------------------------------------------------------

_TEMPLATE_VAR_RE = re.compile(r"\{\{(\w+)\}\}")


def _sync_custom_prompts() -> None:
    """Sync user-defined prompts from the store into the FastMCP registry.

    Each entry with source="custom-prompt" becomes an MCP prompt:
    - logical_key → prompt name (prefixed with "user/")
    - description → prompt description
    - content → template body ({{var}} placeholders become arguments)
    """
    from mcp.server.fastmcp.prompts import Prompt
    from mcp.server.fastmcp.prompts.base import PromptArgument

    entries = store.get_entries(source="custom-prompt")
    pm = mcp._prompt_manager
    # Remove previously synced custom prompts
    to_remove = [name for name in pm._prompts if name.startswith("user/")]
    for name in to_remove:
        del pm._prompts[name]

    for entry in entries:
        key = entry.logical_key or entry.id
        name = f"user/{key}"
        desc = entry.data.get("description", "")
        template = entry.data.get("content", desc)

        # Extract {{var}} placeholders as prompt arguments
        var_names = _TEMPLATE_VAR_RE.findall(template)
        arguments = [
            PromptArgument(name=v, description=f"Value for {v}", required=True)
            for v in dict.fromkeys(var_names)  # deduplicate, preserve order
        ]

        def _make_fn(tmpl: str) -> Any:
            """Create a closure that renders the template."""

            async def _render(**kwargs: str) -> str:
                result = tmpl
                for k, v in kwargs.items():
                    result = result.replace(f"{{{{{k}}}}}", v)
                return result

            return _render

        prompt = Prompt(
            name=name,
            title=None,
            description=desc,
            arguments=arguments if arguments else None,
            fn=_make_fn(template),
            context_kwarg=None,
        )
        pm._prompts[name] = prompt


# Custom prompt sync happens at server start (in main()), not at import time.
# This avoids triggering a DB connection when the module is imported for testing.


def _health_response() -> dict[str, Any]:
    """Build the health check response payload."""
    return {
        "status": "ok",
        "uptime_sec": round(time.monotonic() - _start_time, 1),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "transport": TRANSPORT,
    }


def main() -> None:
    # Sync custom prompts from the store at server start (not at import time)
    _sync_custom_prompts()
    try:
        _run()
    except KeyboardInterrupt:
        print("Shutdown requested — exiting.", flush=True)


def _run() -> None:
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
        from starlette.responses import JSONResponse
        from starlette.types import ASGIApp, Receive, Scope, Send

        inner_app = mcp.streamable_http_app()

        class HealthMiddleware:
            """Serve /health, pass everything else to the MCP app."""

            def __init__(self, app: ASGIApp) -> None:
                self.app = app

            async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
                if scope["type"] == "http" and scope.get("path") == "/health":
                    health_resp = JSONResponse(_health_response())
                    await health_resp(scope, receive, send)
                    return
                await self.app(scope, receive, send)

        health_app = HealthMiddleware(inner_app)

        config = uvicorn.Config(health_app, host=HOST, port=PORT)
        server = uvicorn.Server(config)

        import anyio

        anyio.run(server.serve)
    else:
        mcp.run(transport=TRANSPORT)


if __name__ == "__main__":
    main()
