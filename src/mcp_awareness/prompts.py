"""MCP prompt handlers for the awareness service.

Prompt functions are defined at module level (using helpers.store) so they can
be imported by tests.  register_prompts() wires them into the FastMCP instance.
"""

from __future__ import annotations

import re
from datetime import timedelta
from typing import Any

from mcp.server.fastmcp import FastMCP

from .helpers import _timed, store
from .schema import Entry, EntryType, now_utc, to_iso

_TEMPLATE_VAR_RE = re.compile(r"\{\{(\w+)\}\}")


def _extract_entry_number(desc: str) -> int:
    """Extract 'Entry N' number from description for sorting."""
    m = re.search(r"Entry\s+(\d+)", desc)
    return int(m.group(1)) if m else 99


def _sync_custom_prompts(mcp: FastMCP, store_ref: Any) -> None:
    """Sync user-defined prompts from the store into the FastMCP registry.

    Each entry with source="custom-prompt" becomes an MCP prompt:
    - logical_key -> prompt name (prefixed with "user/")
    - description -> prompt description
    - content -> template body ({{var}} placeholders become arguments)
    """
    from mcp.server.fastmcp.prompts import Prompt
    from mcp.server.fastmcp.prompts.base import PromptArgument

    entries = store_ref.get_entries(source="custom-prompt")
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


# ---------------------------------------------------------------------------
# Prompt handler functions
# ---------------------------------------------------------------------------

# agent_instructions needs access to the mcp instance for _sync_custom_prompts.
# We store a module-level reference that register_prompts() sets.
_mcp_ref: FastMCP | None = None


@_timed
async def agent_instructions() -> str:
    """Compose agent instructions from awareness-prompt entries in the store."""
    if _mcp_ref is not None:
        _sync_custom_prompts(_mcp_ref, store)
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
            parts.append(f"- When: {condition} -> {effect}")

    return "\n".join(parts)


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
# Registration
# ---------------------------------------------------------------------------


def register_prompts(mcp: FastMCP, store_ref: Any) -> None:
    """Register all prompt handlers on the given FastMCP instance."""
    global _mcp_ref
    _mcp_ref = mcp

    mcp.prompt(
        name="agent_instructions",
        description=(
            "Complete agent instructions for using awareness — reading, writing, "
            "tag conventions, quality rules, status maintenance, and resilience. "
            "Built dynamically from the user's stored conventions. "
            "Call this once at conversation start to learn how to use awareness correctly."
        ),
    )(agent_instructions)

    mcp.prompt(
        name="project_context",
        description=(
            "Get everything awareness knows about a project — knowledge entries, "
            "status, active alerts, and relevant patterns. Pass the repo name "
            "(e.g., 'mcp-awareness') to get a composed project briefing."
        ),
    )(project_context)

    mcp.prompt(
        name="system_status",
        description=(
            "Get a composed narrative for a specific system — latest status, "
            "active alerts, and relevant patterns. Pass the source name "
            "(e.g., 'synology-nas')."
        ),
    )(system_status)

    mcp.prompt(
        name="write_guide",
        description=(
            "Interactive guide for writing to awareness — shows existing sources, "
            "tags with usage counts, and entry type distribution. Call before "
            "writing to avoid source/tag drift and duplicates."
        ),
    )(write_guide)

    mcp.prompt(
        name="catchup",
        description=(
            "What changed recently? Shows entries updated in the last N hours "
            "(default 24). Use at conversation start to see what other agents "
            "or platforms have written since you were last active."
        ),
    )(catchup)
