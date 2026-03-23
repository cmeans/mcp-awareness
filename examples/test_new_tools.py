#!/usr/bin/env python3
"""Test the new knowledge layer v2 tools via MCP over HTTP.

Requires the server to be running:
    AWARENESS_DATABASE_URL=postgresql://awareness:awareness-dev@localhost:5432/awareness \
    AWARENESS_TRANSPORT=streamable-http python -m mcp_awareness.server

Usage:
    python examples/test_new_tools.py [--url http://localhost:8420/mcp]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client


async def call_tool(session: ClientSession, name: str, args: dict) -> dict:
    """Call an MCP tool and return parsed JSON result."""
    result = await session.call_tool(name, args)
    text = result.content[0].text
    return json.loads(text)


async def run_tests(url: str) -> None:
    passed = 0
    failed = 0

    def ok(test_name: str, condition: bool, detail: str = "") -> None:
        nonlocal passed, failed
        if condition:
            passed += 1
            print(f"  PASS  {test_name}")
        else:
            failed += 1
            print(f"  FAIL  {test_name}  {detail}")

    async with streamablehttp_client(url) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()

            print("\n=== get_stats (empty) ===")
            stats = await call_tool(session, "get_stats", {})
            ok("empty store", stats["total"] == 0)

            print("\n=== remember (create notes) ===")
            r1 = await call_tool(session, "remember", {
                "source": "personal",
                "tags": ["family"],
                "description": "Mom's birthday is March 15",
                "learned_from": "test-script",
            })
            ok("remember basic", r1["status"] == "ok")
            note_id = r1["id"]

            r2 = await call_tool(session, "remember", {
                "source": "tools",
                "tags": ["backup", "claude-code"],
                "description": "Slash command backup",
                "content": json.dumps({"commands": ["/commit", "/review-pr", "/simplify"]}),
                "content_type": "application/json",
                "learned_from": "test-script",
            })
            ok("remember with content", r2["status"] == "ok")

            print("\n=== get_knowledge (notes included) ===")
            knowledge = await call_tool(session, "get_knowledge", {})
            ok("notes in knowledge", len(knowledge) == 2)

            print("\n=== get_knowledge (filtered by source) ===")
            filtered = await call_tool(session, "get_knowledge", {"source": "personal"})
            ok("filter by source", len(filtered) == 1)
            ok("correct source", filtered[0]["data"]["description"] == "Mom's birthday is March 15")

            print("\n=== get_knowledge (filtered by tags) ===")
            filtered = await call_tool(session, "get_knowledge", {"tags": ["backup"]})
            ok("filter by tags", len(filtered) == 1)
            ok("correct tags", "backup" in filtered[0]["tags"])

            print("\n=== get_knowledge (filtered by entry_type) ===")
            # Add a pattern so we can distinguish
            await call_tool(session, "learn_pattern", {
                "source": "nas",
                "tags": ["infra"],
                "description": "qBittorrent stops on Fridays",
                "effect": "suppress qbittorrent",
            })
            notes_only = await call_tool(session, "get_knowledge", {"entry_type": "note"})
            ok("filter by type", len(notes_only) == 2)

            print("\n=== update_entry (description) ===")
            u1 = await call_tool(session, "update_entry", {
                "entry_id": note_id,
                "description": "Mom's birthday is March 15 — get flowers",
            })
            ok("update ok", u1["status"] == "ok")

            print("\n=== update_entry (tags) ===")
            u2 = await call_tool(session, "update_entry", {
                "entry_id": note_id,
                "tags": ["family", "reminders"],
            })
            ok("update tags ok", u2["status"] == "ok")

            print("\n=== update_entry (verify changelog) ===")
            with_history = await call_tool(session, "get_knowledge", {
                "include_history": "true",
            })
            note = next(e for e in with_history if e["id"] == note_id)
            changelog = note["data"].get("changelog", [])
            ok("changelog has 2 entries", len(changelog) == 2)
            ok("first change tracked desc", "description" in changelog[0]["changed"])
            ok("second change tracked tags", "tags" in changelog[1]["changed"])

            print("\n=== get_knowledge (history stripped by default) ===")
            no_history = await call_tool(session, "get_knowledge", {})
            note_no_hist = next(e for e in no_history if e["id"] == note_id)
            ok("changelog stripped", "changelog" not in note_no_hist["data"])

            print("\n=== get_knowledge (include_history=only) ===")
            only_changed = await call_tool(session, "get_knowledge", {
                "include_history": "only",
            })
            ok("only returns changed entries", len(only_changed) == 1)
            ok("is the updated note", only_changed[0]["id"] == note_id)

            print("\n=== update_entry (immutable type rejected) ===")
            alert = await call_tool(session, "report_alert", {
                "source": "nas",
                "tags": ["infra"],
                "alert_id": "test-cpu",
                "level": "warning",
                "alert_type": "threshold",
                "message": "CPU at 95%",
            })
            u3 = await call_tool(session, "update_entry", {
                "entry_id": alert["id"],
                "description": "should fail",
            })
            ok("alert immutable", u3["status"] == "error")
            ok("error mentions immutable", "immutable" in u3["message"])

            print("\n=== update_entry (not found) ===")
            u4 = await call_tool(session, "update_entry", {
                "entry_id": "nonexistent-id",
                "description": "nope",
            })
            ok("not found error", u4["status"] == "error")

            print("\n=== get_stats ===")
            stats = await call_tool(session, "get_stats", {})
            ok("total count", stats["total"] == 4)  # 2 notes + 1 pattern + 1 alert
            ok("note count", stats["entries"]["note"] == 2)
            ok("pattern count", stats["entries"]["pattern"] == 1)
            ok("alert count", stats["entries"]["alert"] == 1)
            ok("sources present", "personal" in stats["sources"])

            print("\n=== get_tags ===")
            tags = await call_tool(session, "get_tags", {})
            tag_names = [t["tag"] for t in tags]
            ok("family tag exists", "family" in tag_names)
            ok("infra tag exists", "infra" in tag_names)
            ok("backup tag exists", "backup" in tag_names)
            # infra should have count 2 (pattern + alert)
            infra = next(t for t in tags if t["tag"] == "infra")
            ok("infra count correct", infra["count"] == 2)

            print("\n=== get_briefing (sanity check) ===")
            briefing = await call_tool(session, "get_briefing", {})
            ok("briefing has attention_needed", "attention_needed" in briefing)

            # Clean up — resolve the alert
            await call_tool(session, "report_alert", {
                "source": "nas",
                "tags": ["infra"],
                "alert_id": "test-cpu",
                "level": "warning",
                "alert_type": "threshold",
                "message": "CPU at 95%",
                "resolved": True,
            })

    print(f"\n{'='*40}")
    print(f"Results: {passed} passed, {failed} failed")
    if failed:
        sys.exit(1)
    print("All tests passed!")


def main() -> None:
    parser = argparse.ArgumentParser(description="Test knowledge layer v2 tools via MCP")
    parser.add_argument("--url", default="http://localhost:8420/mcp", help="MCP server URL")
    args = parser.parse_args()
    asyncio.run(run_tests(args.url))


if __name__ == "__main__":
    main()
