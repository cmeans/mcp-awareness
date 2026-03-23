#!/usr/bin/env python3
"""Populate the awareness store with demo data.

This demonstrates the full range of what mcp-awareness stores: system
monitoring, personal knowledge, time-limited context, and preferences.
In production, agents write via MCP tools and edge processes report via
HTTP — this script writes directly for testing and demonstration.

Usage:
    python examples/simulate_edge.py [--data-dir ./data]
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Add src to path for direct execution
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from mcp_awareness.collator import generate_briefing
from mcp_awareness.schema import Entry, EntryType, make_id, now_iso
from mcp_awareness.postgres_store import PostgresStore


def simulate(dsn: str = "postgresql://awareness:awareness-dev@localhost:5432/awareness") -> None:
    store = PostgresStore(dsn)

    # -----------------------------------------------------------------------
    # 1. System awareness — edge process reporting status and alerts
    # -----------------------------------------------------------------------
    print("=== System Awareness (edge process reporting) ===\n")

    print("1. Reporting NAS status...")
    store.upsert_status(
        "home-nas",
        ["infra", "nas"],
        {
            "metrics": {
                "cpu": {"usage_pct": 34},
                "memory": {"usage_pct": 71},
                "disk_io": {"busy_pct": 82},
            },
            "inventory": {
                "docker": {
                    "running": ["plex", "home-assistant", "pihole"],
                    "stopped": [],
                }
            },
            "ttl_sec": 3600,
        },
    )
    print("   Status reported: healthy\n")

    print("2. Firing structural alert (container stopped)...")
    store.upsert_alert(
        "home-nas",
        ["infra", "nas", "docker"],
        "struct-pihole-stopped",
        {
            "alert_id": "struct-pihole-stopped",
            "level": "warning",
            "alert_type": "structural",
            "message": "pihole container is not running — DNS resolution may be affected",
            "diagnostics": {
                "container": "pihole",
                "exit_code": 137,
                "last_running": "2 hours ago",
            },
            "resolved": False,
        },
    )

    briefing = generate_briefing(store)
    print(f"   Briefing: {briefing['summary']}")
    print(f"   Attention needed: {briefing['attention_needed']}\n")

    # -----------------------------------------------------------------------
    # 2. Personal knowledge — agents write what they learn from conversation
    # -----------------------------------------------------------------------
    print("=== Personal Knowledge (written by agents) ===\n")

    knowledge_entries = [
        {
            "source": "home-network",
            "tags": ["infra", "network", "home"],
            "description": "Home network runs Ubiquiti UniFi. VLAN 10 is IoT devices, VLAN 20 is trusted. Guest network is isolated.",
            "learned_from": "conversation",
        },
        {
            "source": "user-preferences",
            "tags": ["preferences", "alerts"],
            "description": "Prefers one-sentence warnings for non-critical alerts. Only expand to a paragraph for critical issues.",
            "learned_from": "conversation",
        },
        {
            "source": "user-work",
            "tags": ["career", "projects"],
            "description": "Full-stack developer focused on platform engineering and AI agent tooling. Primary language is Python.",
            "learned_from": "conversation",
        },
        {
            "source": "family",
            "tags": ["family", "scheduling"],
            "description": "Family calendar (Google Calendar) is the source of truth for scheduling. Always check before suggesting meeting times.",
            "learned_from": "conversation",
        },
    ]

    for i, entry in enumerate(knowledge_entries, start=3):
        print(f"{i}. Storing knowledge: {entry['description'][:60]}...")
        store.add(
            Entry(
                id=make_id(),
                type=EntryType.PATTERN,
                source=entry["source"],
                tags=entry["tags"],
                created=now_iso(),
                updated=now_iso(),
                expires=None,
                data={
                    "description": entry["description"],
                    "conditions": {},
                    "effect": "",
                    "learned_from": entry["learned_from"],
                },
            )
        )
    print()

    # -----------------------------------------------------------------------
    # 3. Time-limited context — events and temporary situations
    # -----------------------------------------------------------------------
    print("=== Context Entries (time-limited) ===\n")

    now = datetime.now(timezone.utc)
    context_entries = [
        {
            "source": "home-infra",
            "tags": ["infra", "home", "renovation"],
            "description": "Kitchen renovation in progress — expect Home Assistant sensors in kitchen to go offline intermittently.",
            "expires_days": 60,
        },
        {
            "source": "home-infra",
            "tags": ["infra", "network", "isp"],
            "description": "Switched ISP to fiber on March 1. Still monitoring stability — occasional drops between 2-4am.",
            "expires_days": 30,
        },
        {
            "source": "family",
            "tags": ["family", "events"],
            "description": "Annual family reunion is July 12 at Lake Geneva. Travel plans not yet booked.",
            "expires_days": 120,
        },
    ]

    for i, entry in enumerate(context_entries, start=7):
        expires = (now + timedelta(days=entry["expires_days"])).isoformat()
        print(f"{i}. Adding context: {entry['description'][:60]}...")
        store.add(
            Entry(
                id=make_id(),
                type=EntryType.CONTEXT,
                source=entry["source"],
                tags=entry["tags"],
                created=now_iso(),
                updated=now_iso(),
                expires=expires,
                data={"description": entry["description"]},
            )
        )
    print()

    # -----------------------------------------------------------------------
    # 4. Preferences — portable across agents
    # -----------------------------------------------------------------------
    print("=== Preferences ===\n")

    print("10. Setting preference: alert_verbosity = one_sentence_warnings")
    store.upsert_preference(
        key="alert_verbosity",
        scope="global",
        tags=[],
        data={
            "key": "alert_verbosity",
            "value": "one_sentence_warnings",
            "scope": "global",
        },
    )
    print()

    # -----------------------------------------------------------------------
    # 5. Final briefing
    # -----------------------------------------------------------------------
    print("=== Final Briefing ===")
    briefing = generate_briefing(store)
    print(json.dumps(briefing, indent=2))

    # Summary
    all_knowledge = store.get_knowledge()
    print(f"\n=== Store Summary ===")
    print(f"   Knowledge entries: {len(all_knowledge)}")
    print(f"   Active alerts: {len(store.get_active_alerts())}")
    print(f"   Attention needed: {briefing['attention_needed']}")
    print(
        "\nTry asking an agent: 'What do you know about my home network?' "
        "or 'What needs my attention?'"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Populate the awareness store with demo data"
    )
    parser.add_argument(
        "--data-dir", default="./data", help="Data directory for the store"
    )
    args = parser.parse_args()
    simulate(args.data_dir)
