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

"""MCP tool handlers for the awareness service.

All ``@mcp.tool`` registrations live here.  The module is imported by
``server.py`` **after** the ``mcp`` instance is created, so the decorators
bind to the live FastMCP object.

Mutable state (``store``, ``mcp``, ``_generate_embedding``, etc.) is accessed
via ``_srv.<name>`` so that test monkeypatching on ``server_mod`` is visible
at call time.
"""

from __future__ import annotations

import json
import logging
from datetime import timedelta
from typing import Any

from . import server as _srv
from .collator import generate_briefing
from .embeddings import compose_embedding_text, text_hash
from .helpers import (
    DEFAULT_QUERY_LIMIT,
    VALID_ALERT_LEVELS,
    VALID_ALERT_TYPES,
    VALID_URGENCY,
    _error_response,
    _paginate,
    _parse_entry_type,
    _timed,
    _validate_enum,
    _validate_pagination,
    _validate_timestamp,
)
from .language import ISO_639_1_TO_REGCONFIG, SIMPLE, detect_language_iso, resolve_language
from .schema import Entry, EntryType, make_id, now_utc, to_iso

logger = logging.getLogger(__name__)


def _check_unsupported_language(text: str, resolved: str) -> None:
    """Fire an info alert when lingua detects a language not in the regconfig mapping.

    Only fires when: resolved == 'simple' (fallback) AND lingua detected a specific
    language that has no regconfig. This signals demand for a language the server
    doesn't support, informing the Phase 3 reactivation decision.

    Note: this re-runs lingua detection via detect_language_iso() after
    resolve_language() already ran it. The double call is intentional —
    lingua caches internally, the cost is negligible, and threading the
    raw ISO code through resolve_language would complicate its API for
    a rare-path optimization.
    """
    if resolved != SIMPLE:
        return
    iso = detect_language_iso(text)
    if iso is None or iso in ISO_639_1_TO_REGCONFIG:
        return
    alert_id = f"unsupported-language-{iso}"
    try:
        _srv.store.upsert_alert(
            _srv._owner_id(),
            source="mcp-awareness",
            tags=["language", "unsupported"],
            alert_id=alert_id,
            data={
                "alert_id": alert_id,
                "level": "info",
                "alert_type": "structural",
                "message": f"Detected language '{iso}' has no Postgres regconfig — "
                f"entry stored with 'simple' fallback. If this language appears "
                f"frequently, consider adding support in a future release.",
                "resolved": False,
                "detected_iso": iso,
            },
        )
    except Exception:
        logger.debug("Failed to fire unsupported-language alert for %s", iso, exc_info=True)


# ---------------------------------------------------------------------------
# Read tools (mirrors of resources, for MCP clients that only support tools)
# ---------------------------------------------------------------------------


@_srv.mcp.tool()
@_timed
async def get_briefing() -> str:
    """Get the awareness briefing. Call this at conversation start.
    Returns a compact summary (~200 tokens all-clear, ~500 with issues).
    If attention_needed is true, mention the suggested_mention or compose
    your own from the source headlines. If false, nothing to report.
    Pre-filtered through patterns and suppressions — no further processing needed.
    This tool always returns structured JSON. If you receive an unstructured
    error, the failure is in the transport or platform layer, not in awareness."""
    return json.dumps(generate_briefing(_srv.store, _srv._owner_id()), indent=2)


@_srv.mcp.tool()
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
    pv = _validate_pagination(limit, offset)
    if isinstance(pv, str):
        _error_response(
            "invalid_parameter",
            pv,
            retryable=False,
            param="limit" if "limit" in pv else "offset",
            value=limit if "limit" in pv else offset,
        )
    limit, offset = pv
    assert limit is not None  # _validate_pagination guarantees a default
    since_dt = _validate_timestamp(since, "since")
    alerts = _srv.store.get_active_alerts(
        _srv._owner_id(), source, since=since_dt, limit=limit + 1, offset=offset
    )
    _srv._log_reads(alerts[:limit], "get_alerts")
    if mode == "list":
        page = _paginate([a.to_list_dict() for a in alerts], limit, offset)
    else:
        page = _paginate([a.to_dict() for a in alerts], limit, offset)
    return json.dumps(page, indent=2)


@_srv.mcp.tool()
@_timed
async def get_status(source: str) -> str:
    """Get full status for a specific source including metrics and inventory.
    Call when the briefing indicates issues with a source or user asks
    about a specific system. This tool always returns structured JSON.
    If you receive an unstructured error, the failure is in the transport
    or platform layer, not in awareness."""
    entry = _srv.store.get_latest_status(_srv._owner_id(), source)
    if entry:
        return json.dumps(entry.to_dict(), indent=2)
    _error_response(
        "not_found",
        f"No status found for source: '{source}'",
        retryable=False,
        param="source",
        value=source,
    )


@_srv.mcp.tool()
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
    language: str | None = None,
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
    language: optional ISO 639-1 code to filter entries by their stored language
    (e.g., language='fr' returns only French entries). Useful for "show me all
    entries in language X" queries.
    Results are sorted by most recently updated first (or by relevance if hint is set).
    This tool always returns JSON with a status field or an entry list.
    If you receive an unstructured error, the failure is in the transport
    or platform layer, not in awareness."""
    pv = _validate_pagination(limit, offset)
    if isinstance(pv, str):
        _error_response(
            "invalid_parameter",
            pv,
            retryable=False,
            param="limit" if "limit" in pv else "offset",
            value=limit if "limit" in pv else offset,
        )
    limit, offset = pv
    assert limit is not None  # _validate_pagination guarantees a default
    since_dt = _validate_timestamp(since, "since")
    until_dt = _validate_timestamp(until, "until")
    created_after_dt = _validate_timestamp(created_after, "created_after")
    created_before_dt = _validate_timestamp(created_before, "created_before")
    et = _parse_entry_type(entry_type)
    from .language import ISO_639_1_TO_REGCONFIG, SIMPLE, iso_to_regconfig

    lang_regconfig: str | None = None
    if language:
        normalized = language.strip().lower()
        if normalized == SIMPLE:
            lang_regconfig = SIMPLE
        elif normalized not in ISO_639_1_TO_REGCONFIG:
            _error_response(
                "invalid_parameter",
                f"Unknown language code: '{language}'. Use ISO 639-1 codes "
                f"(e.g., 'en', 'fr', 'de'). Use language='simple' to filter "
                f"entries with no detected language.",
                retryable=False,
                param="language",
                value=language,
            )
        else:
            lang_regconfig = iso_to_regconfig(language)
    entries = _srv.store.get_knowledge(
        _srv._owner_id(),
        tags=tags,
        include_history=include_history,
        since=since_dt,
        until=until_dt,
        source=source,
        entry_type=et,
        learned_from=learned_from,
        created_after=created_after_dt,
        created_before=created_before_dt,
        language=lang_regconfig,
        limit=limit + 1,
        offset=offset,
    )
    # Semantic re-ranking: if hint is provided and embeddings are available,
    # re-order results by cosine similarity to the hint text.
    similarity_map: dict[str, float] = {}
    if hint and entries:
        provider = _srv._get_embedding_provider()
        if provider.is_available():
            try:
                hint_vec = provider.embed([hint])
                if hint_vec:
                    hint_et = et
                    scored = _srv.store.semantic_search(
                        _srv._owner_id(),
                        embedding=hint_vec[0],
                        model=provider.model_name,
                        query_text=hint,
                        query_language=SIMPLE,
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
                logger.debug("Hint re-ranking failed", exc_info=True)

    # Log reads after re-ranking so we track exactly what's returned
    _srv._log_reads(entries[:limit], "get_knowledge")

    if mode == "list":
        read_counts = _srv.store.get_read_counts(_srv._owner_id(), [e.id for e in entries])
        result_items = []
        for e in entries:
            d = e.to_list_dict()
            counts = read_counts.get(e.id, {})
            d["read_count"] = counts.get("read_count", 0)
            d["last_read"] = counts.get("last_read")
            if e.id in similarity_map:
                d["similarity"] = round(similarity_map[e.id], 4)
            result_items.append(d)
        page = _paginate(result_items, limit, offset)
        return json.dumps(page, indent=2)
    items = []
    for e in entries:
        d = e.to_dict()
        if e.id in similarity_map:
            d["similarity"] = round(similarity_map[e.id], 4)
        items.append(d)
    page = _paginate(items, limit, offset)
    return json.dumps(page, indent=2)


@_srv.mcp.tool()
@_timed
async def get_suppressions() -> str:
    """Get active alert suppressions with expiry times and escalation settings.
    The briefing already applies suppressions — call this to show the user
    what's currently being suppressed."""
    entries = _srv.store.get_active_suppressions(_srv._owner_id())
    return json.dumps([e.to_dict() for e in entries], indent=2)


# ---------------------------------------------------------------------------
# Write tools
# ---------------------------------------------------------------------------


@_srv.mcp.tool()
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
    entry = _srv.store.upsert_status(_srv._owner_id(), source, tags, data)
    _srv._generate_embedding(entry)
    return json.dumps({"status": "ok", "id": entry.id, "source": source})


@_srv.mcp.tool()
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
    _validate_enum(level, "level", VALID_ALERT_LEVELS)
    _validate_enum(alert_type, "alert_type", VALID_ALERT_TYPES)
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
    entry = _srv.store.upsert_alert(_srv._owner_id(), source, tags, alert_id, data)
    _srv._generate_embedding(entry)
    action = "resolved" if resolved else "reported"
    return json.dumps({"status": "ok", "id": entry.id, "action": action, "alert_id": alert_id})


@_srv.mcp.tool()
@_timed
async def learn_pattern(
    source: str,
    tags: list[str],
    description: str,
    conditions: dict[str, Any] | None = None,
    effect: str | None = None,
    learned_from: str = "conversation",
    language: str | None = None,
) -> str:
    """Record an if/then operational rule that the alert collator uses for matching.
    Use ONLY when there is a clear condition -> effect relationship:
    e.g., 'When qBittorrent restarts on Fridays, expect high CPU for 10 minutes'.
    The conditions and effect fields drive automatic alert suppression and pattern
    matching — they are not just metadata.
    NOT for general facts, project notes, or personal knowledge — use remember for those.
    Quick test: does it have a "when X happens, expect Y"? -> learn_pattern. Otherwise -> remember.
    Any agent can write; any agent can read. Knowledge is portable across platforms.

    Returns:
        JSON: {"status": "ok", "id": "<uuid>"}

    If you receive an unstructured error, the failure is in the transport
    or platform layer, not in awareness."""
    now = now_utc()
    text_for_detect = f"{description} {effect or ''}"
    resolved_lang = resolve_language(explicit=language, text_for_detection=text_for_detect)
    _check_unsupported_language(text_for_detect, resolved_lang)
    entry = Entry(
        id=make_id(),
        type=EntryType.PATTERN,
        source=source,
        tags=tags,
        created=now,
        expires=None,
        data={
            "description": description,
            "conditions": conditions or {},
            "effect": effect or "",
            "learned_from": learned_from,
        },
        language=resolved_lang,
    )
    _srv.store.add(_srv._owner_id(), entry)
    _srv._generate_embedding(entry)
    return json.dumps({"status": "ok", "id": entry.id})


@_srv.mcp.tool()
@_timed
async def remember(
    source: str,
    tags: list[str],
    description: str,
    content: str | dict[str, Any] | list[Any] | None = None,
    content_type: str = "text/plain",
    learned_from: str = "conversation",
    logical_key: str | None = None,
    language: str | None = None,
) -> str:
    """Store permanent knowledge — facts that will still be true in 30 days.
    This is the default tool for recording what you learn. Use it for personal facts,
    project notes, design decisions, config snapshots, preferences, or anything
    worth knowing long-term.
    Quick test: still true in 30 days? -> remember. Happening now, will become stale?
    -> add_context. Has a "when X, expect Y" rule? -> learn_pattern.
    description is a short summary; content is the optional payload (text, JSON, etc.).
    content_type is a MIME type (default text/plain). Set learned_from to your platform.
    logical_key is an optional identifier for upsert behavior — if a note with the
    same source + logical_key exists, it will be updated in place (with changelog
    tracking) instead of creating a duplicate. Use for living documents like
    project status notes.

    Returns:
        JSON: {"status": "ok", "id": "<uuid>"} for normal calls.
        When logical_key is provided, additionally includes
        "action": "created" | "updated" — presence of the field signals
        the upsert path was taken; "updated" means logical_key matched
        an existing entry.

    If you receive an unstructured error, the failure is in the transport
    or platform layer, not in awareness."""
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
    text_for_detect = f"{description} {content or ''}"
    resolved_lang = resolve_language(explicit=language, text_for_detection=text_for_detect)
    _check_unsupported_language(text_for_detect, resolved_lang)
    entry = Entry(
        id=make_id(),
        type=EntryType.NOTE,
        source=source,
        tags=tags,
        created=now,
        expires=None,
        data=data,
        logical_key=logical_key,
        language=resolved_lang,
    )
    if logical_key:
        result, created = _srv.store.upsert_by_logical_key(
            _srv._owner_id(), source, logical_key, entry
        )
        _srv._generate_embedding(result)
        action = "created" if created else "updated"
        return json.dumps({"status": "ok", "id": result.id, "action": action})
    _srv.store.add(_srv._owner_id(), entry)
    _srv._generate_embedding(entry)
    return json.dumps({"status": "ok", "id": entry.id})


@_srv.mcp.tool()
@_timed
async def update_entry(
    entry_id: str,
    description: str | None = None,
    tags: list[str] | None = None,
    source: str | None = None,
    content: str | dict[str, Any] | list[Any] | None = None,
    content_type: str | None = None,
    language: str | None = None,
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
    if language is not None:
        from .language import iso_to_regconfig

        updates["language"] = iso_to_regconfig(language)
    if not updates:
        _error_response(
            "invalid_parameter",
            "No fields to update — provide at least one of: "
            "description, tags, source, content, content_type, language",
            retryable=False,
            param="content",
        )
    result = _srv.store.update_entry(_srv._owner_id(), entry_id, updates)
    if result is None:
        _error_response(
            "not_found",
            f"Entry not found or type is immutable (status/alert/suppression): '{entry_id}'",
            retryable=False,
            param="entry_id",
            value=entry_id,
        )
    _srv._generate_embedding(result)
    return json.dumps(
        {
            "status": "ok",
            "id": result.id,
            "updated": to_iso(result.updated) if result.updated else None,
        }
    )


@_srv.mcp.tool()
@_timed
async def get_stats() -> str:
    """Get summary statistics: entry counts by type, list of sources, total count.
    Call before get_knowledge to decide whether to pull everything or filter.
    This tool always returns structured JSON. If you receive an unstructured
    error, the failure is in the transport or platform layer, not in awareness."""
    return json.dumps(_srv.store.get_stats(_srv._owner_id()), indent=2)


@_srv.mcp.tool()
@_timed
async def get_tags() -> str:
    """Get all tags in use with usage counts, sorted by count descending.
    Use this to discover existing tags before creating new ones — prevents
    tag drift (e.g., 'infrastructure' vs 'infra'). This tool always returns
    structured JSON. If you receive an unstructured error, the failure is in
    the transport or platform layer, not in awareness."""
    return json.dumps(_srv.store.get_tags(_srv._owner_id()), indent=2)


@_srv.mcp.tool()
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
    _validate_enum(level, "level", VALID_ALERT_LEVELS)
    if duration_minutes < 1:
        _error_response(
            "invalid_parameter",
            "duration_minutes must be at least 1",
            retryable=False,
            param="duration_minutes",
            value=duration_minutes,
        )
    now = now_utc()
    expires = now + timedelta(minutes=duration_minutes)
    entry = Entry(
        id=make_id(),
        type=EntryType.SUPPRESSION,
        source=source or "",
        tags=tags or [],
        created=now,
        expires=expires,
        data={
            "metric": metric,
            "suppress_level": level,
            "escalation_override": escalation_override,
            "reason": reason,
        },
    )
    _srv.store.add(_srv._owner_id(), entry)
    return json.dumps({"status": "ok", "id": entry.id, "expires": to_iso(expires)})


@_srv.mcp.tool()
@_timed
async def add_context(
    source: str,
    tags: list[str],
    description: str,
    expires_days: int = 30,
    language: str | None = None,
) -> str:
    """Record something happening now that will become stale — auto-expires
    after the specified duration (default 30 days).
    Use for current events, milestones, temporary states, or anything with
    a natural shelf life: 'sdb replaced, RAID rebuilding', 'PR #45 merged',
    'Alice moving this week', 'construction on Ashland through April'.
    Quick test: still true in 30 days? -> remember instead. Happening now,
    will become stale? -> add_context. Any agent on any platform can read this."""
    if expires_days < 1:
        _error_response(
            "invalid_parameter",
            "expires_days must be at least 1",
            retryable=False,
            param="expires_days",
            value=expires_days,
        )
    now = now_utc()
    expires = now + timedelta(days=expires_days)
    resolved_lang = resolve_language(explicit=language, text_for_detection=description)
    _check_unsupported_language(description, resolved_lang)
    entry = Entry(
        id=make_id(),
        type=EntryType.CONTEXT,
        source=source,
        tags=tags,
        created=now,
        expires=expires,
        data={"description": description},
        language=resolved_lang,
    )
    _srv.store.add(_srv._owner_id(), entry)
    _srv._generate_embedding(entry)
    return json.dumps({"status": "ok", "id": entry.id, "expires": to_iso(expires)})


@_srv.mcp.tool()
@_timed
async def set_preference(
    key: str,
    value: str,
    scope: str = "global",
) -> str:
    """Set a presentation preference. Portable across agent platforms.
    Use this for preferences like alert_verbosity='one_sentence_warnings'
    or check_frequency='first_turn_only'. These are portable —
    any agent on any platform reads the same preferences.

    Returns:
        JSON: {"status": "ok", "id": "<uuid>", "key": "<key>", "scope": "<scope>"}
        key + scope form the compound upsert handle and are retained so
        callers can confirm which preference was acted on. The stored
        value is not echoed back."""
    entry = _srv.store.upsert_preference(
        _srv._owner_id(),
        key=key,
        scope=scope,
        tags=[],
        data={"key": key, "value": value, "scope": scope},
    )
    return json.dumps({"status": "ok", "id": entry.id, "key": key, "scope": scope})


@_srv.mcp.tool()
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
        _srv.store.soft_delete_by_id(_srv._owner_id(), entry_id)
        return json.dumps(
            {
                "status": "acknowledged",
                "entry_id": entry_id,
                "recoverable_days": 30,
                "note": "If the entry was not found, no action was taken.",
            }
        )
    if tags:
        if not confirm:
            # Use AND logic to match soft_delete_by_tags behavior
            all_entries = _srv.store.get_entries(_srv._owner_id(), tags=tags)
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
        count = _srv.store.soft_delete_by_tags(_srv._owner_id(), tags)
        return json.dumps(
            {
                "status": "ok",
                "trashed": count,
                "tags": tags,
                "recoverable_days": 30,
            }
        )
    if not source:
        _error_response(
            "invalid_parameter",
            "Provide entry_id, tags, or source",
            retryable=False,
        )
    et = _parse_entry_type(entry_type)
    if not confirm:
        entries = _srv.store.get_entries(_srv._owner_id(), entry_type=et, source=source)
        return json.dumps(
            {
                "status": "dry_run",
                "would_trash": len(entries),
                "source": source,
                "entry_type": entry_type,
                "message": "Set confirm=True to move to trash. Show the user this count first.",
            }
        )
    count = _srv.store.soft_delete_by_source(_srv._owner_id(), source, et)
    return json.dumps(
        {
            "status": "ok",
            "trashed": count,
            "source": source,
            "entry_type": entry_type,
            "recoverable_days": 30,
        }
    )


@_srv.mcp.tool()
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
        restored = _srv.store.restore_by_id(_srv._owner_id(), entry_id)
        return json.dumps(
            {
                "status": "ok" if restored else "not_found",
                "restored": 1 if restored else 0,
                "entry_id": entry_id,
            }
        )
    if tags:
        count = _srv.store.restore_by_tags(_srv._owner_id(), tags)
        return json.dumps({"status": "ok", "restored": count, "tags": tags})
    _error_response(
        "invalid_parameter",
        "Provide entry_id or tags",
        retryable=False,
    )


@_srv.mcp.tool()
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
    pv = _validate_pagination(limit, offset)
    if isinstance(pv, str):
        _error_response(
            "invalid_parameter",
            pv,
            retryable=False,
            param="limit" if "limit" in pv else "offset",
            value=limit if "limit" in pv else offset,
        )
    limit, offset = pv
    assert limit is not None  # _validate_pagination guarantees a default
    since_dt = _validate_timestamp(since, "since")
    entries = _srv.store.get_deleted(
        _srv._owner_id(), since=since_dt, limit=limit + 1, offset=offset
    )
    if mode == "list":
        page = _paginate([e.to_list_dict() for e in entries], limit, offset)
    else:
        page = _paginate([e.to_dict() for e in entries], limit, offset)
    return json.dumps(page, indent=2)


# ---------------------------------------------------------------------------
# Read / action tracking tools
# ---------------------------------------------------------------------------


@_srv.mcp.tool()
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

    Returns:
        JSON: {"status": "ok", "id": "<action_record_id>",
               "entry_id": "<source_entry_id>", "action": "<action_label>",
               "timestamp": "<iso8601>"}
        action is the caller-supplied effect label (the substance of the
        action record, not echoed payload). entry_id is retained as the
        primary handle linking the action back to its source entry.

    If you receive an unstructured error, the failure is in the transport
    or platform layer, not in awareness."""
    result = _srv.store.log_action(
        _srv._owner_id(),
        entry_id=entry_id,
        action=action,
        platform=platform,
        detail=detail,
        tags=tags,
    )
    if result.get("status") == "error":
        return json.dumps(result)
    return json.dumps(
        {
            "status": "ok",
            "id": result["id"],
            "entry_id": result["entry_id"],
            "action": result["action"],
            "timestamp": result["timestamp"],
        }
    )


@_srv.mcp.tool()
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
    if limit is None:
        limit = DEFAULT_QUERY_LIMIT
    since_dt = _validate_timestamp(since, "since")
    reads = _srv.store.get_reads(
        _srv._owner_id(), entry_id=entry_id, since=since_dt, platform=platform, limit=limit + 1
    )
    page = _paginate(reads, limit, None)
    return json.dumps(page, indent=2)


@_srv.mcp.tool()
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
    if limit is None:
        limit = DEFAULT_QUERY_LIMIT
    since_dt = _validate_timestamp(since, "since")
    actions = _srv.store.get_actions(
        _srv._owner_id(),
        entry_id=entry_id,
        since=since_dt,
        platform=platform,
        tags=tags,
        limit=limit + 1,
    )
    page = _paginate(actions, limit, None)
    return json.dumps(page, indent=2)


@_srv.mcp.tool()
@_timed
async def get_unread(since: str | None = None, limit: int | None = None) -> str:
    """Get entries with zero reads — cleanup candidates and dead knowledge.
    since: optional — only consider reads after this timestamp, so
    'unread in the last 30 days' is possible even if something was read
    6 months ago.
    limit: max entries to return (default 100).
    Returns entry metadata (list mode format).
    This tool always returns structured JSON."""
    if limit is None:
        limit = DEFAULT_QUERY_LIMIT
    since_dt = _validate_timestamp(since, "since")
    entries = _srv.store.get_unread(_srv._owner_id(), since=since_dt, limit=limit + 1)
    page = _paginate([e.to_list_dict() for e in entries], limit, None)
    return json.dumps(page, indent=2)


@_srv.mcp.tool()
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
    if limit is None:
        limit = DEFAULT_QUERY_LIMIT
    since_dt = _validate_timestamp(since, "since")
    activity = _srv.store.get_activity(
        _srv._owner_id(), since=since_dt, platform=platform, limit=limit + 1
    )
    page = _paginate(activity, limit, None)
    return json.dumps(page, indent=2)


# ---------------------------------------------------------------------------
# Intention tools
# ---------------------------------------------------------------------------


@_srv.mcp.tool()
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
    language: str | None = None,
) -> str:
    """Create a todo, reminder, or planned action — anything the user intends to do.
    Use this for tasks, errands, goals, follow-ups, and scheduled work. Intentions
    have a lifecycle: pending -> fired -> active -> completed/snoozed/cancelled.
    goal: what needs to happen (e.g., 'pick up milk', 'review PR #47', 'call dentist').
    deliver_at: ISO 8601 timestamp — when to surface this. Required for time-based
    reminders. Omit for open-ended todos or intentions triggered by other conditions
    (location, events) in the future.
    constraints: optional preferences or requirements (e.g., 'organic, budget-conscious').
    urgency: 'low', 'normal', or 'high'. High-urgency intentions surface more prominently.
    recurrence: reserved for future use. Currently only one-shot intentions are supported.
    This tool always returns structured JSON."""
    _validate_enum(urgency, "urgency", VALID_URGENCY)
    now = now_utc()
    deliver_at_dt = _validate_timestamp(deliver_at, "deliver_at")
    resolved_lang = resolve_language(explicit=language, text_for_detection=goal)
    _check_unsupported_language(goal, resolved_lang)
    entry = Entry(
        id=make_id(),
        type=EntryType.INTENTION,
        source=source,
        tags=tags,
        created=now,
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
        language=resolved_lang,
    )
    _srv.store.add(_srv._owner_id(), entry)
    return json.dumps({"status": "ok", "id": entry.id, "state": "pending"}, indent=2)


@_srv.mcp.tool()
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
    if limit is None:
        limit = DEFAULT_QUERY_LIMIT
    entries = _srv.store.get_intentions(
        _srv._owner_id(), state=state, source=source, tags=tags, limit=limit + 1
    )
    if mode == "list":
        page = _paginate([e.to_list_dict() for e in entries], limit, None)
    else:
        page = _paginate([e.to_dict() for e in entries], limit, None)
    return json.dumps(page, indent=2)


@_srv.mcp.tool()
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

    Returns:
        JSON: {"status": "ok", "id": "<entry_id>"}
        id is the caller-supplied entry_id, retained as the lookup handle
        (not a server-generated id). The new state is not echoed back —
        the caller already knows what they sent, and validation errors
        surface as structured errors before the store is touched."""
    from .schema import INTENTION_STATES

    _validate_enum(state, "state", INTENTION_STATES)
    result = _srv.store.update_intention_state(_srv._owner_id(), entry_id, state, reason)
    if result is None:
        _error_response(
            "not_found",
            "Intention not found",
            retryable=False,
            param="entry_id",
            value=entry_id,
        )
    return json.dumps({"status": "ok", "id": entry_id})


@_srv.mcp.tool()
@_timed
async def search(
    query: str,
    source: str | None = None,
    tags: list[str] | None = None,
    entry_type: str | None = None,
    since: str | None = None,
    until: str | None = None,
    limit: int = 10,
    mode: str | None = None,
    language: str | None = None,
) -> str:
    """Search knowledge by meaning — hybrid vector + full-text search with RRF fusion.
    Use when tag-based filtering (get_knowledge) isn't specific enough,
    or when you need to find entries related to a concept without knowing exact tags.
    Example: search(query="retirement planning") finds entries
    about 401k, pension, financial goals — even if not tagged that way.
    Combines with filters: source, tags, entry_type, since, until.
    Returns entries sorted by relevance with similarity scores.
    Requires an embedding provider (AWARENESS_EMBEDDING_PROVIDER env var).
    language: optional ISO 639-1 code for query-time language resolution (e.g., 'en').
    Affects the FTS branch — lexical matching uses language-specific stemming.
    mode: omit for full entries, 'list' for metadata only + similarity."""
    from .language import iso_to_regconfig

    limit = max(1, min(limit, 100))
    since_dt = _validate_timestamp(since, "since")
    until_dt = _validate_timestamp(until, "until")
    et = _parse_entry_type(entry_type)
    query_language = iso_to_regconfig(language) if language else SIMPLE
    provider = _srv._get_embedding_provider()
    if not provider.is_available():
        _error_response(
            "unavailable",
            "Semantic search requires an embedding provider. "
            "Set AWARENESS_EMBEDDING_PROVIDER=ollama and ensure Ollama is running.",
            retryable=True,
        )
    # Generate query embedding
    try:
        vectors = provider.embed([query])
        if not vectors:
            _error_response("unavailable", "Failed to generate query embedding", retryable=True)
    except Exception as exc:
        _error_response("unavailable", f"Embedding error: {exc}", retryable=True)

    results = _srv.store.semantic_search(
        _srv._owner_id(),
        embedding=vectors[0],
        model=provider.model_name,
        query_text=query,
        query_language=query_language,
        entry_type=et,
        source=source,
        tags=tags,
        since=since_dt,
        until=until_dt,
        limit=limit + 1,
    )
    _srv._log_reads([e for e, _ in results[:limit]], "search")
    if mode == "list":
        items = []
        for entry, score in results:
            d = entry.to_list_dict()
            d["similarity"] = round(score, 4)
            items.append(d)
        page = _paginate(items, limit, None)
        return json.dumps(page, indent=2)
    items = []
    for entry, score in results:
        d = entry.to_dict()
        d["similarity"] = round(score, 4)
        items.append(d)
    page = _paginate(items, limit, None)
    return json.dumps(page, indent=2)


@_srv.mcp.tool()
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
    language: str | None = None,
) -> str:
    """Deprecated alias for search. Use search() instead.
    This tool will be removed in a future release."""
    result: str = await search(
        query=query,
        source=source,
        tags=tags,
        entry_type=entry_type,
        since=since,
        until=until,
        limit=limit,
        mode=mode,
        language=language,
    )
    return result


@_srv.mcp.tool()
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
    provider = _srv._get_embedding_provider()
    if not provider.is_available():
        _error_response(
            "unavailable",
            "Backfill requires an embedding provider. Set AWARENESS_EMBEDDING_PROVIDER=ollama.",
            retryable=True,
        )

    # Phase 1: entries without embeddings
    oid = _srv._owner_id()
    missing = _srv.store.get_entries_without_embeddings(oid, provider.model_name, limit=limit)
    new_count = 0
    if missing:
        texts = [compose_embedding_text(e) for e in missing]
        hashes = [text_hash(t) for t in texts]
        try:
            vectors = provider.embed(texts)
        except Exception:  # pragma: no cover
            logger.debug("Backfill embed failed", exc_info=True)
            vectors = []
        for entry, h, vec in zip(missing, hashes, vectors, strict=False):
            try:
                _srv.store.upsert_embedding(
                    oid, entry.id, provider.model_name, provider.dimensions, h, vec
                )
                new_count += 1
            except Exception:  # pragma: no cover
                logger.debug("Backfill upsert failed for entry %s", entry.id, exc_info=True)
                continue

    # Phase 2: stale embeddings (text changed since embedding)
    stale = _srv.store.get_stale_embeddings(oid, provider.model_name, limit=limit)
    refreshed_count = 0
    if stale:
        texts = [compose_embedding_text(e) for e in stale]
        hashes = [text_hash(t) for t in texts]
        try:
            vectors = provider.embed(texts)
        except Exception:  # pragma: no cover
            logger.debug("Backfill refresh embed failed", exc_info=True)
            vectors = []
        for entry, h, vec in zip(stale, hashes, vectors, strict=False):
            try:
                _srv.store.upsert_embedding(
                    oid, entry.id, provider.model_name, provider.dimensions, h, vec
                )
                refreshed_count += 1
            except Exception:  # pragma: no cover
                logger.debug("Backfill refresh upsert failed for entry %s", entry.id, exc_info=True)
                continue

    remaining = len(_srv.store.get_entries_without_embeddings(oid, provider.model_name, limit=1))
    return json.dumps(
        {
            "status": "ok",
            "new_embeddings": new_count,
            "refreshed_embeddings": refreshed_count,
            "remaining": remaining,
        }
    )


@_srv.mcp.tool()
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
    oid = _srv._owner_id()
    entry = _srv.store.get_entry_by_id(oid, entry_id)
    if entry is None:
        _error_response(
            "not_found",
            f"Entry not found: '{entry_id}'",
            retryable=False,
            param="entry_id",
            value=entry_id,
        )

    # Forward: entries this entry references
    forward_ids: list[str] = [rid for rid in entry.data.get("related_ids", []) if rid != entry_id]
    forward = _srv.store.get_entries_by_ids(oid, forward_ids) if forward_ids else []

    # Reverse: entries that reference this entry via JSONB containment
    reverse = _srv.store.get_referencing_entries(oid, entry_id)

    # Deduplicate (an entry could be in both directions)
    seen = set()
    related = []
    for e in forward + reverse:
        if e.id not in seen:
            seen.add(e.id)
            related.append(e)

    _srv._log_reads(related, "get_related")
    if mode == "list":
        return json.dumps([e.to_list_dict() for e in related], indent=2)
    return json.dumps([e.to_dict() for e in related], indent=2)
