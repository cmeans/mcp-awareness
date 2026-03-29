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

"""MCP prompt handlers for the awareness service.

All ``@mcp.prompt`` registrations live here.  The module is imported by
``server.py`` **after** the ``mcp`` instance is created, so the decorators
bind to the live FastMCP object.

Mutable state (``store``, ``mcp``, ``_sync_custom_prompts``) is accessed
via ``_srv.<name>`` so that test monkeypatching on ``server_mod`` is visible
at call time.
"""

from __future__ import annotations

import re
from datetime import timedelta

from . import server as _srv
from .helpers import _timed
from .schema import Entry, EntryType, now_utc, to_iso


def _extract_entry_number(desc: str) -> int:
    """Extract 'Entry N' number from description for sorting."""
    m = re.search(r"Entry\s+(\d+)", desc)
    return int(m.group(1)) if m else 99


# ---------------------------------------------------------------------------
# Prompts (discoverable agent instructions, built from store data)
# ---------------------------------------------------------------------------


@_srv.mcp.prompt(
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
    _srv._sync_custom_prompts()
    entries = _srv.store.get_knowledge(_srv._owner_id(), tags=["memory-prompt"])
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


@_srv.mcp.prompt(
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
    entries = _srv.store.get_knowledge(_srv._owner_id(), tags=[repo_name])
    alerts = _srv.store.get_active_alerts(_srv._owner_id())
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


@_srv.mcp.prompt(
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
    oid = _srv._owner_id()
    status = _srv.store.get_latest_status(oid, source)
    alerts = _srv.store.get_active_alerts(oid, source=source)
    patterns = _srv.store.get_patterns(oid, source=source)

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
        parts.append(f"- Last report: {to_iso(status.updated or status.created)}")

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


@_srv.mcp.prompt(
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
    oid = _srv._owner_id()
    stats = _srv.store.get_stats(oid)
    tags = _srv.store.get_tags(oid)

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


@_srv.mcp.prompt(
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
    oid = _srv._owner_id()
    recent = _srv.store.get_knowledge(oid, include_history="true", since=since)
    recent_alerts = _srv.store.get_active_alerts(oid, since=since)

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
