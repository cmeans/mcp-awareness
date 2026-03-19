#!/usr/bin/env python3
"""Simulate an edge process writing to the awareness store.

This demonstrates how edge processes interact with mcp-awareness by
writing status reports and alerts directly to the store. In production,
edge processes would call the MCP tools over stdio or HTTP — this script
writes directly for testing and demonstration purposes.

Usage:
    python examples/simulate_edge.py [--data-dir ./data]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Add src to path for direct execution
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from mcp_awareness.collator import generate_briefing
from mcp_awareness.schema import Entry, EntryType, make_id, now_iso
from mcp_awareness.store import AwarenessStore


def simulate(data_dir: str = "./data") -> None:
    store = AwarenessStore(Path(data_dir) / "awareness.db")

    print("=== Simulating edge process: synology-nas ===\n")

    # 1. Report healthy status
    print("1. Reporting healthy status...")
    store.upsert_status(
        "synology-nas",
        ["infra", "nas", "seedbox"],
        {
            "metrics": {
                "cpu": {"usage_pct": 34},
                "memory": {"usage_pct": 71},
                "disk_io": {"busy_pct": 82},
            },
            "inventory": {
                "docker": {
                    "running": ["qbittorrent", "plex", "download-station"],
                    "stopped": [],
                }
            },
            "ttl_sec": 3600,
        },
    )

    briefing = generate_briefing(store)
    print(f"   Briefing: {briefing['summary']}")
    print(f"   Attention needed: {briefing['attention_needed']}\n")

    # 2. Fire a warning alert
    print("2. Firing CPU warning alert...")
    store.upsert_alert(
        "synology-nas",
        ["infra", "nas"],
        "cpu-warn-001",
        {
            "alert_id": "cpu-warn-001",
            "level": "warning",
            "alert_type": "threshold",
            "metric": "cpu_pct",
            "message": "CPU at 92% for 5+ minutes",
            "diagnostics": {
                "top_processes": [
                    {"name": "qbittorrent", "cpu_pct": 74.3},
                    {"name": "plex", "cpu_pct": 8.1},
                ],
            },
            "resolved": False,
        },
    )

    briefing = generate_briefing(store)
    print(f"   Briefing: {briefing['summary']}")
    print(f"   Suggested mention: {briefing.get('suggested_mention', 'N/A')}\n")

    # 3. Learn a pattern
    print("3. Learning pattern: qBittorrent maintenance on Fridays...")
    store.add(
        Entry(
            id=make_id(),
            type=EntryType.PATTERN,
            source="synology-nas",
            tags=["infra", "nas"],
            created=now_iso(),
            updated=now_iso(),
            expires=None,
            data={
                "description": "qBittorrent sometimes stopped for maintenance on Fridays",
                "conditions": {"day_of_week": "friday"},
                "effect": "suppress qbittorrent_stopped",
                "learned_from": "conversation",
            },
        )
    )
    print(f"   Patterns stored: {len(store.get_patterns('synology-nas'))}\n")

    # 4. Resolve the alert
    print("4. Resolving CPU alert...")
    store.upsert_alert(
        "synology-nas",
        ["infra", "nas"],
        "cpu-warn-001",
        {
            "alert_id": "cpu-warn-001",
            "level": "warning",
            "alert_type": "threshold",
            "message": "CPU at 92% for 5+ minutes",
            "resolved": True,
        },
    )

    briefing = generate_briefing(store)
    print(f"   Briefing: {briefing['summary']}")
    print(f"   Attention needed: {briefing['attention_needed']}\n")

    # 5. Print full briefing
    print("=== Final briefing ===")
    print(json.dumps(briefing, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Simulate an edge process")
    parser.add_argument("--data-dir", default="./data", help="Data directory for the store")
    args = parser.parse_args()
    simulate(args.data_dir)
