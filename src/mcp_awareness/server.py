"""FastMCP server — resources + tools for the awareness service.

Transport is selected via the AWARENESS_TRANSPORT environment variable:
  - "stdio" (default): stdin/stdout, for direct MCP client integration
  - "streamable-http": HTTP server on AWARENESS_HOST:AWARENESS_PORT/mcp
"""

from __future__ import annotations

import concurrent.futures
import functools
import json
import os
import pathlib
import re
import time
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from mcp.server.fastmcp import FastMCP

from .collator import generate_briefing
from .embeddings import (
    EmbeddingProvider,
    compose_embedding_text,
    create_provider,
    should_embed,
    text_hash,
)
from .postgres_store import PostgresStore
from .schema import Entry, EntryType, ensure_dt, make_id, now_utc, parse_iso, to_iso
from .store import Store

_start_time = time.monotonic()

# Valid values for enum-like parameters
VALID_ALERT_LEVELS = {"warning", "critical"}
VALID_ALERT_TYPES = {"threshold", "structural", "baseline"}
VALID_URGENCY = {"low", "normal", "high"}


_VALID_ENTRY_TYPES = [e.value for e in EntryType]


def _parse_entry_type(entry_type: str | None) -> tuple[EntryType | None, str | None]:
    """Parse entry_type string. Returns (value, None) or (None, error)."""
    if not entry_type:
        return None, None
    try:
        return EntryType(entry_type), None
    except ValueError:
        return None, f"Invalid entry_type: {entry_type!r}. Valid: {_VALID_ENTRY_TYPES}"


def _validate_pagination(
    limit: int | None, offset: int | None
) -> tuple[int | None, int | None] | str:
    """Validate and clamp pagination params. Returns (limit, offset) or error string."""
    if limit is not None and limit < 0:
        return "limit must be non-negative"
    if offset is not None and offset < 0:
        return "offset must be non-negative"
    return limit, offset


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

# Embedding provider — optional, configured via env vars
EMBEDDING_PROVIDER = os.environ.get("AWARENESS_EMBEDDING_PROVIDER", "")
EMBEDDING_MODEL = os.environ.get("AWARENESS_EMBEDDING_MODEL", "nomic-embed-text")
OLLAMA_URL = os.environ.get("AWARENESS_OLLAMA_URL", "http://ollama:11434")
EMBEDDING_DIMENSIONS = int(os.environ.get("AWARENESS_EMBEDDING_DIMENSIONS", "768"))

_embedding_provider: EmbeddingProvider | None = None


def _get_embedding_provider() -> EmbeddingProvider:
    """Lazy-init the embedding provider from env vars."""
    global _embedding_provider
    if _embedding_provider is None:
        _embedding_provider = create_provider(
            provider=EMBEDDING_PROVIDER,
            model=EMBEDDING_MODEL,
            ollama_url=OLLAMA_URL,
            dimensions=EMBEDDING_DIMENSIONS,
        )
    return _embedding_provider


# Thread pool for background embedding generation — max 2 workers to avoid
# overwhelming Ollama while keeping writes non-blocking.
_embedding_pool = concurrent.futures.ThreadPoolExecutor(max_workers=2, thread_name_prefix="embed")


def _do_embed(
    entry_id: str,
    entry_source: str,
    entry_tags: list[str],
    entry_data: dict[str, Any],
    entry_type_val: str,
) -> None:
    """Actual embedding work — runs in thread pool with its own DB connection.

    Uses a dedicated connection to avoid racing with the main thread's
    shared connection (same pattern as _do_cleanup in PostgresStore).
    """
    try:
        provider = _get_embedding_provider()
        if not provider.is_available():
            return
        entry = Entry(
            id=entry_id,
            type=EntryType(entry_type_val),
            source=entry_source,
            tags=entry_tags,
            created=now_utc(),
            updated=now_utc(),
            expires=None,
            data=entry_data,
        )
        text = compose_embedding_text(entry)
        h = text_hash(text)
        vectors = provider.embed([text])
        if vectors:
            # Use a dedicated connection for the background write to avoid
            # racing with the main thread's shared connection.
            import psycopg

            with psycopg.connect(store.dsn) as conn:
                vector_literal = "[" + ",".join(str(v) for v in vectors[0]) + "]"
                conn.execute(
                    "INSERT INTO embeddings (entry_id, model, dimensions, text_hash, embedding) "
                    "VALUES (%s, %s, %s, %s, %s::vector) "
                    "ON CONFLICT (entry_id, model) DO UPDATE SET "
                    "embedding = EXCLUDED.embedding, text_hash = EXCLUDED.text_hash, "
                    "dimensions = EXCLUDED.dimensions, created = now()",
                    (entry_id, provider.model_name, provider.dimensions, h, vector_literal),
                )
                conn.commit()
    except Exception:
        pass  # Backfill will catch failures


def _generate_embedding(entry: Entry) -> None:
    """Submit embedding generation to background thread pool. Never blocks."""
    if not should_embed(entry):
        return
    entry_type_val = entry.type.value if isinstance(entry.type, EntryType) else entry.type
    _embedding_pool.submit(
        _do_embed, entry.id, entry.source, list(entry.tags), dict(entry.data), entry_type_val
    )


def _log_reads(entries: list[Any], tool_name: str) -> None:
    """Log that entries were read. Fire-and-forget — never blocks the response."""
    try:
        ids = [e.id for e in entries if hasattr(e, "id")]
        if ids:
            store.log_read(ids, tool_used=tool_name)
    except Exception:
        pass  # Read logging must never break the tool response


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


_INSTRUCTIONS_PATH = pathlib.Path(__file__).parent / "instructions.md"
_INSTRUCTIONS = _INSTRUCTIONS_PATH.read_text(encoding="utf-8").strip()

mcp = FastMCP(
    name="mcp-awareness",
    host=HOST,
    port=PORT,
    instructions=_INSTRUCTIONS,
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
    if since is not None and not since:
        return json.dumps({"error": "since cannot be empty; omit or provide an ISO 8601 timestamp"})
    since_dt = ensure_dt(since) if since else None
    alerts = store.get_active_alerts(source, since=since_dt, limit=limit, offset=offset)
    _log_reads(alerts, "get_alerts")
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
    until: str | None = None,
    learned_from: str | None = None,
    created_after: str | None = None,
    created_before: str | None = None,
    hint: str | None = None,
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
    until: ISO 8601 timestamp — only return entries updated before this time.
    Combine with since for date ranges (e.g., "what happened in March?").
    learned_from: filter by platform that created the entry (e.g., 'claude-code',
    'claude.ai', 'conversation'). Useful when multiple platforms write entries.
    created_after: ISO 8601 timestamp — filter by creation time (not last update).
    created_before: ISO 8601 timestamp — filter by creation time (not last update).
    Use created_after/created_before when you care about when knowledge was first
    recorded, not when it was last modified.
    hint: natural language phrase to re-rank results by semantic similarity.
    Requires an embedding provider. Tag/source filters still apply — hint just
    reorders the results so the most relevant appear first. Example:
    get_knowledge(tags=["finance"], hint="retirement savings") returns all
    finance entries but with retirement-related ones ranked first.
    mode: omit for full entries, 'list' for metadata only (id, type, source,
    description, tags, created, updated — no content or changelog). Use 'list'
    to orient before pulling full entries.
    Use limit/offset for pagination (e.g., limit=10, offset=0 for first page).
    Results are sorted by most recently updated first (or by relevance if hint is set).
    This tool always returns JSON with a status field or an entry list.
    If you receive an unstructured error, the failure is in the transport
    or platform layer, not in awareness."""
    if since is not None and not since:
        return json.dumps({"error": "since cannot be empty; omit or provide an ISO 8601 timestamp"})
    pv = _validate_pagination(limit, offset)
    if isinstance(pv, str):
        return json.dumps({"error": pv})
    limit, offset = pv
    since_dt = ensure_dt(since) if since else None
    until_dt = ensure_dt(until) if until else None
    created_after_dt = ensure_dt(created_after) if created_after else None
    created_before_dt = ensure_dt(created_before) if created_before else None
    et, et_err = _parse_entry_type(entry_type)
    if et_err:
        return json.dumps({"error": et_err})
    entries = store.get_knowledge(
        tags=tags,
        include_history=include_history,
        since=since_dt,
        until=until_dt,
        source=source,
        entry_type=et,
        learned_from=learned_from,
        created_after=created_after_dt,
        created_before=created_before_dt,
        limit=limit,
        offset=offset,
    )
    _log_reads(entries, "get_knowledge")

    # Semantic re-ranking: if hint is provided and embeddings are available,
    # re-order results by cosine similarity to the hint text.
    similarity_map: dict[str, float] = {}
    if hint and entries:
        provider = _get_embedding_provider()
        if provider.is_available():
            try:
                hint_vec = provider.embed([hint])
                if hint_vec:
                    hint_et = et
                    scored = store.semantic_search(
                        embedding=hint_vec[0],
                        model=provider.model_name,
                        source=source,
                        tags=tags,
                        entry_type=hint_et,
                        since=since_dt,
                        until=until_dt,
                        limit=len(entries) + 10,
                    )
                    similarity_map = {e.id: s for e, s in scored}
                    # Re-sort: entries with embeddings by similarity (desc),
                    # entries without embeddings at the end
                    entries.sort(key=lambda e: similarity_map.get(e.id, -1.0), reverse=True)
            except Exception:  # pragma: no cover
                pass  # Fall back to default ordering

    if mode == "list":
        read_counts = store.get_read_counts([e.id for e in entries])
        result = []
        for e in entries:
            d = e.to_list_dict()
            counts = read_counts.get(e.id, {})
            d["read_count"] = counts.get("read_count", 0)
            d["last_read"] = counts.get("last_read")
            if e.id in similarity_map:
                d["similarity"] = round(similarity_map[e.id], 4)
            result.append(d)
        return json.dumps(result, indent=2)
    items = []
    for e in entries:
        d = e.to_dict()
        if e.id in similarity_map:
            d["similarity"] = round(similarity_map[e.id], 4)
        items.append(d)
    return json.dumps(items, indent=2)


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
    _generate_embedding(entry)
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
    if level not in VALID_ALERT_LEVELS:
        return json.dumps(
            {"error": f"invalid level '{level}', must be one of: {sorted(VALID_ALERT_LEVELS)}"}
        )
    if alert_type not in VALID_ALERT_TYPES:
        return json.dumps(
            {
                "error": f"invalid alert_type '{alert_type}',"
                f" must be one of: {sorted(VALID_ALERT_TYPES)}"
            }
        )
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
    _generate_embedding(entry)
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
    """Record an if/then operational rule that the alert collator uses for matching.
    Use ONLY when there is a clear condition → effect relationship:
    e.g., 'When qBittorrent restarts on Fridays, expect high CPU for 10 minutes'.
    The conditions and effect fields drive automatic alert suppression and pattern
    matching — they are not just metadata.
    NOT for general facts, project notes, or personal knowledge — use remember for those.
    Quick test: does it have a "when X happens, expect Y"? → learn_pattern. Otherwise → remember.
    Any agent can write; any agent can read. Knowledge is portable across platforms.
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
    _generate_embedding(entry)
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
    """Store permanent knowledge — facts that will still be true in 30 days.
    This is the default tool for recording what you learn. Use it for personal facts,
    project notes, design decisions, config snapshots, preferences, or anything
    worth knowing long-term.
    Quick test: still true in 30 days? → remember. Happening now, will become stale?
    → add_context. Has a "when X, expect Y" rule? → learn_pattern.
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
        # Pydantic may deserialize JSON strings into dicts/lists before our
        # str type hint is checked. Serialize back to ensure content is always a string.
        if not isinstance(content, str):
            content = json.dumps(content)
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
        _generate_embedding(result)
        action = "created" if created else "updated"
        return json.dumps(
            {"status": "ok", "id": result.id, "action": action, "description": description}
        )
    store.add(entry)
    _generate_embedding(entry)
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
        if not isinstance(content, str):
            content = json.dumps(content)
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
    _generate_embedding(result)
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
    if level not in VALID_ALERT_LEVELS:
        return json.dumps(
            {"error": f"invalid level '{level}', must be one of: {sorted(VALID_ALERT_LEVELS)}"}
        )
    if duration_minutes < 1:
        return json.dumps({"error": "duration_minutes must be at least 1"})
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
    """Record something happening now that will become stale — auto-expires
    after the specified duration (default 30 days).
    Use for current events, milestones, temporary states, or anything with
    a natural shelf life: 'sdb replaced, RAID rebuilding', 'PR #45 merged',
    'Alice moving this week', 'construction on Ashland through April'.
    Quick test: still true in 30 days? → remember instead. Happening now,
    will become stale? → add_context. Any agent on any platform can read this."""
    if expires_days < 1:
        return json.dumps({"error": "expires_days must be at least 1"})
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
    _generate_embedding(entry)
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
            # Use AND logic to match soft_delete_by_tags behavior
            all_entries = store.get_entries(tags=tags)
            tag_set = set(tags)
            matching = [e for e in all_entries if tag_set.issubset(set(e.tags))]
            return json.dumps(
                {
                    "status": "dry_run",
                    "would_trash": len(matching),
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
    et, et_err = _parse_entry_type(entry_type)
    if et_err:
        return json.dumps({"status": "error", "message": et_err})
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
    if since is not None and not since:
        return json.dumps({"error": "since cannot be empty; omit or provide an ISO 8601 timestamp"})
    since_dt = ensure_dt(since) if since else None
    entries = store.get_deleted(since=since_dt, limit=limit, offset=offset)
    if mode == "list":
        return json.dumps([e.to_list_dict() for e in entries], indent=2)
    return json.dumps([e.to_dict() for e in entries], indent=2)


# ---------------------------------------------------------------------------
# Read / action tracking tools
# ---------------------------------------------------------------------------


@mcp.tool()
@_timed
async def acted_on(
    entry_id: str,
    action: str,
    platform: str | None = None,
    detail: str | None = None,
    tags: list[str] | None = None,
) -> str:
    """Record that you took a concrete action because of an awareness entry.
    Call this when you use an entry to do something: implement a feature,
    create an issue, answer a question, make a decision.
    entry_id: the entry that motivated the action.
    action: what you did (e.g., 'created GitHub issue #24', 'used for context').
    platform: your platform name (e.g., 'claude-code', 'claude.ai').
    detail: optional structured reference (PR URL, issue number, etc.).
    tags: optional — defaults to copying tags from the referenced entry.
    This tool always returns structured JSON. If you receive an unstructured
    error, the failure is in the transport or platform layer, not in awareness."""
    result = store.log_action(
        entry_id=entry_id, action=action, platform=platform, detail=detail, tags=tags
    )
    if result.get("status") == "error":
        return json.dumps(result)
    return json.dumps({"status": "ok", **result}, indent=2)


@mcp.tool()
@_timed
async def get_reads(
    entry_id: str | None = None,
    since: str | None = None,
    platform: str | None = None,
    limit: int | None = None,
) -> str:
    """Get read history for entries. Shows which entries have been accessed,
    when, and by which tool. Use to investigate consumption patterns or
    verify that knowledge is being used.
    All params optional. No params = recent reads across all entries.
    This tool always returns structured JSON."""
    since_dt = ensure_dt(since) if since else None
    reads = store.get_reads(entry_id=entry_id, since=since_dt, platform=platform, limit=limit)
    return json.dumps(reads, indent=2)


@mcp.tool()
@_timed
async def get_actions(
    entry_id: str | None = None,
    since: str | None = None,
    platform: str | None = None,
    tags: list[str] | None = None,
    limit: int | None = None,
) -> str:
    """Get action history — what agents did because of awareness entries.
    The audit trail for knowledge-to-action causality.
    Filter by entry_id, time, platform, or tags.
    This tool always returns structured JSON."""
    since_dt = ensure_dt(since) if since else None
    actions = store.get_actions(
        entry_id=entry_id, since=since_dt, platform=platform, tags=tags, limit=limit
    )
    return json.dumps(actions, indent=2)


@mcp.tool()
@_timed
async def get_unread(since: str | None = None) -> str:
    """Get entries with zero reads — cleanup candidates and dead knowledge.
    since: optional — only consider reads after this timestamp, so
    'unread in the last 30 days' is possible even if something was read
    6 months ago.
    Returns entry metadata (list mode format).
    This tool always returns structured JSON."""
    since_dt = ensure_dt(since) if since else None
    entries = store.get_unread(since=since_dt)
    return json.dumps([e.to_list_dict() for e in entries], indent=2)


@mcp.tool()
@_timed
async def get_activity(
    since: str | None = None,
    platform: str | None = None,
    limit: int | None = None,
) -> str:
    """Get combined read + action activity feed, chronologically.
    Shows all engagement with the store — reads and actions interleaved.
    Useful for inter-agent coordination ('what did other agents access?')
    and auditing.
    This tool always returns structured JSON."""
    since_dt = ensure_dt(since) if since else None
    activity = store.get_activity(since=since_dt, platform=platform, limit=limit)
    return json.dumps(activity, indent=2)


# ---------------------------------------------------------------------------
# Intention tools
# ---------------------------------------------------------------------------


@mcp.tool()
@_timed
async def remind(
    goal: str,
    source: str,
    tags: list[str],
    deliver_at: str | None = None,
    constraints: str | None = None,
    urgency: str = "normal",
    recurrence: str | None = None,
    learned_from: str = "conversation",
) -> str:
    """Create a todo, reminder, or planned action — anything the user intends to do.
    Use this for tasks, errands, goals, follow-ups, and scheduled work. Intentions
    have a lifecycle: pending → fired → active → completed/snoozed/cancelled.
    goal: what needs to happen (e.g., 'pick up milk', 'review PR #47', 'call dentist').
    deliver_at: ISO 8601 timestamp — when to surface this. Required for time-based
    reminders. Omit for open-ended todos or intentions triggered by other conditions
    (location, events) in the future.
    constraints: optional preferences or requirements (e.g., 'organic, budget-conscious').
    urgency: 'low', 'normal', or 'high'. High-urgency intentions surface more prominently.
    recurrence: reserved for future use. Currently only one-shot intentions are supported.
    This tool always returns structured JSON."""
    if urgency not in VALID_URGENCY:
        return json.dumps(
            {"error": f"invalid urgency '{urgency}', must be one of: {sorted(VALID_URGENCY)}"}
        )
    now = now_utc()
    deliver_at_dt = ensure_dt(deliver_at) if deliver_at else None
    entry = Entry(
        id=make_id(),
        type=EntryType.INTENTION,
        source=source,
        tags=tags,
        created=now,
        updated=now,
        expires=None,
        data={
            "goal": goal,
            "state": "pending",
            "deliver_at": to_iso(deliver_at_dt) if deliver_at_dt else None,
            "constraints": constraints,
            "urgency": urgency,
            "recurrence": recurrence,
            "learned_from": learned_from,
        },
    )
    store.add(entry)
    return json.dumps({"status": "ok", "id": entry.id, "state": "pending"}, indent=2)


@mcp.tool()
@_timed
async def get_intentions(
    state: str | None = None,
    source: str | None = None,
    tags: list[str] | None = None,
    mode: str | None = None,
    limit: int | None = None,
) -> str:
    """Get intentions, optionally filtered by state, source, or tags.
    Valid states: 'pending', 'fired', 'active', 'completed', 'snoozed', 'cancelled'.
    mode: omit for full entries, 'list' for metadata only.
    This tool always returns structured JSON."""
    entries = store.get_intentions(state=state, source=source, tags=tags, limit=limit)
    if mode == "list":
        return json.dumps([e.to_list_dict() for e in entries], indent=2)
    return json.dumps([e.to_dict() for e in entries], indent=2)


@mcp.tool()
@_timed
async def update_intention(
    entry_id: str,
    state: str,
    reason: str | None = None,
) -> str:
    """Transition an intention to a new state.
    Valid states: 'fired', 'active', 'completed', 'snoozed', 'cancelled'.
    reason: optional explanation (e.g., 'completed at Mariano\\'s', 'not today').
    Use 'active' when you've started working on it, 'completed' when done,
    'snoozed' to defer, 'cancelled' to permanently dismiss.
    This tool always returns structured JSON."""
    from .schema import INTENTION_STATES

    if state not in INTENTION_STATES:
        return json.dumps(
            {"status": "error", "message": f"Invalid state: {state}. Valid: {INTENTION_STATES}"}
        )
    result = store.update_intention_state(entry_id, state, reason)
    if result is None:
        return json.dumps({"status": "error", "message": "Intention not found"})
    return json.dumps({"status": "ok", "id": entry_id, "state": state, "reason": reason}, indent=2)


@mcp.tool()
@_timed
async def semantic_search(
    query: str,
    source: str | None = None,
    tags: list[str] | None = None,
    entry_type: str | None = None,
    since: str | None = None,
    until: str | None = None,
    limit: int = 10,
    mode: str | None = None,
) -> str:
    """Search knowledge by meaning using semantic similarity.
    Use when tag-based filtering (get_knowledge) isn't specific enough,
    or when you need to find entries related to a concept without knowing exact tags.
    Example: semantic_search(query="retirement planning") finds entries
    about 401k, pension, financial goals — even if not tagged that way.
    Combines with filters: source, tags, entry_type, since, until.
    Returns entries sorted by relevance with similarity scores.
    Requires an embedding provider (AWARENESS_EMBEDDING_PROVIDER env var).
    mode: omit for full entries, 'list' for metadata only + similarity."""
    et, et_err = _parse_entry_type(entry_type)
    if et_err:
        return json.dumps({"status": "error", "message": et_err})
    provider = _get_embedding_provider()
    if not provider.is_available():
        return json.dumps(
            {
                "status": "error",
                "message": (
                    "Semantic search requires an embedding provider. "
                    "Set AWARENESS_EMBEDDING_PROVIDER=ollama and ensure Ollama is running."
                ),
            }
        )
    # Generate query embedding
    try:
        vectors = provider.embed([query])
        if not vectors:
            return json.dumps({"status": "error", "message": "Failed to generate query embedding"})
    except Exception as exc:
        return json.dumps({"status": "error", "message": f"Embedding error: {exc}"})

    since_dt = parse_iso(since) if since else None
    until_dt = parse_iso(until) if until else None

    results = store.semantic_search(
        embedding=vectors[0],
        model=provider.model_name,
        entry_type=et,
        source=source,
        tags=tags,
        since=since_dt,
        until=until_dt,
        limit=limit,
    )
    _log_reads([e for e, _ in results], "semantic_search")
    if mode == "list":
        items = []
        for entry, score in results:
            d = entry.to_list_dict()
            d["similarity"] = round(score, 4)
            items.append(d)
        return json.dumps(items, indent=2)
    items = []
    for entry, score in results:
        d = entry.to_dict()
        d["similarity"] = round(score, 4)
        items.append(d)
    return json.dumps(items, indent=2)


@mcp.tool()
@_timed
async def backfill_embeddings(
    limit: int = 50,
) -> str:
    """Generate embeddings for entries that don't have one yet.
    Also re-embeds entries whose content changed since their last embedding.
    Call this after enabling an embedding provider to index existing knowledge,
    or periodically to catch up on stale embeddings.
    Returns counts of new and refreshed embeddings.
    Requires an embedding provider (AWARENESS_EMBEDDING_PROVIDER env var)."""
    provider = _get_embedding_provider()
    if not provider.is_available():
        return json.dumps(
            {
                "status": "error",
                "message": (
                    "Backfill requires an embedding provider. "
                    "Set AWARENESS_EMBEDDING_PROVIDER=ollama."
                ),
            }
        )

    # Phase 1: entries without embeddings
    missing = store.get_entries_without_embeddings(provider.model_name, limit=limit)
    new_count = 0
    for entry in missing:
        try:
            text = compose_embedding_text(entry)
            h = text_hash(text)
            vectors = provider.embed([text])
            if vectors:
                store.upsert_embedding(
                    entry.id, provider.model_name, provider.dimensions, h, vectors[0]
                )
                new_count += 1
        except Exception:  # pragma: no cover
            continue

    # Phase 2: stale embeddings (text changed since embedding)
    stale = store.get_stale_embeddings(provider.model_name, limit=limit)
    refreshed_count = 0
    for entry in stale:
        try:
            text = compose_embedding_text(entry)
            h = text_hash(text)
            vectors = provider.embed([text])
            if vectors:
                store.upsert_embedding(
                    entry.id, provider.model_name, provider.dimensions, h, vectors[0]
                )
                refreshed_count += 1
        except Exception:  # pragma: no cover
            continue

    remaining = len(store.get_entries_without_embeddings(provider.model_name, limit=1))
    return json.dumps(
        {
            "status": "ok",
            "new_embeddings": new_count,
            "refreshed_embeddings": refreshed_count,
            "remaining": remaining,
        }
    )


@mcp.tool()
@_timed
async def get_related(
    entry_id: str,
    mode: str | None = None,
) -> str:
    """Get entries related to a given entry (bidirectional).
    Returns entries that this entry references via related_ids in its data,
    plus entries that reference this entry in their related_ids.
    Use this to explore connections between decisions, context, patterns,
    and intentions. Convention: store related_ids as a list of entry IDs
    in the data field when using remember or learn_pattern.
    mode: omit for full entries, 'list' for metadata only."""
    entry = store.get_entry_by_id(entry_id)
    if entry is None:
        return json.dumps({"status": "error", "message": f"Entry not found: {entry_id}"})

    # Forward: entries this entry references
    forward_ids: list[str] = entry.data.get("related_ids", [])
    forward = [store.get_entry_by_id(rid) for rid in forward_ids if rid != entry_id]
    forward = [e for e in forward if e is not None]

    # Reverse: entries that reference this entry via JSONB containment
    reverse = store.get_referencing_entries(entry_id)

    # Deduplicate (an entry could be in both directions)
    seen = set()
    related = []
    for e in forward + reverse:
        if e.id not in seen:
            seen.add(e.id)
            related.append(e)

    _log_reads(related, "get_related")
    if mode == "list":
        return json.dumps([e.to_list_dict() for e in related], indent=2)
    return json.dumps([e.to_dict() for e in related], indent=2)


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
    # Access _prompts dict for deletion only — no public remove API exists in FastMCP.
    # add_prompt() is used for insertion (public API).
    prompts_dict = mcp._prompt_manager._prompts
    to_remove = [name for name in prompts_dict if name.startswith("user/")]
    for name in to_remove:
        del prompts_dict[name]

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
        # Force overwrite — add_prompt() skips duplicates, but we need
        # to replace prompts whose content changed in the store.
        prompts_dict[name] = prompt


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
