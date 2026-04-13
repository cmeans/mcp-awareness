#!/usr/bin/env python3
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

"""RLS-aware language backfill for existing entries.

The Alembic migration (l7g8h9i0j1k2) runs through a raw SQLAlchemy
connection without RLS context, so it sees zero rows on databases with
FORCE ROW LEVEL SECURITY enabled. This script sets the RLS context
per-owner and runs the same backfill logic.

Usage:
    AWARENESS_DATABASE_URL="host=... dbname=... user=... password=..."
    python scripts/backfill_language.py [--dry-run]

Idempotent — safe to re-run. Only updates entries where language='simple'
and lingua detects a non-simple language.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

BATCH_SIZE = 100


def _compose_text(data: dict) -> str:
    """Build detection text from entry fields."""
    parts: list[str] = []
    if desc := data.get("description"):
        parts.append(str(desc))
    if content := data.get("content"):
        parts.append(str(content))
    if goal := data.get("goal"):
        parts.append(str(goal))
    if effect := data.get("effect"):
        parts.append(str(effect))
    if message := data.get("message"):
        parts.append(str(message))
    return " ".join(parts)


def main() -> None:
    parser = argparse.ArgumentParser(description="RLS-aware language backfill")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be updated")
    parser.add_argument("--owner", type=str, default=None, help="Owner ID to backfill")
    args = parser.parse_args()

    dsn = os.environ.get("AWARENESS_DATABASE_URL", "")
    if not dsn:
        print("Error: AWARENESS_DATABASE_URL is required.", file=sys.stderr)
        sys.exit(1)

    import psycopg

    from mcp_awareness.language import resolve_language

    conn = psycopg.connect(dsn)

    # Get all distinct owner_ids — this query bypasses RLS because it's
    # a superuser/table-owner query on the owner_id column itself.
    # If RLS blocks even this, fall back to the default owner.
    with conn.transaction():
        conn.execute("SELECT set_config('app.current_user', 'system', true)")
        owners = [
            row[0]
            for row in conn.execute("SELECT DISTINCT owner_id FROM entries").fetchall()
        ]

    if not owners:
        # RLS blocked even the owner list — try env or positional arg
        fallback = os.environ.get("AWARENESS_DEFAULT_OWNER", "system")
        owners = [fallback]
        print(f"Could not query owner_ids (RLS). Trying default: {owners[0]}")

    # Allow explicit owner override via --owner
    if hasattr(args, "owner") and args.owner:
        owners = [args.owner]
        print(f"Using explicit owner: {args.owner}")

    print(f"Found {len(owners)} owner(s): {owners}")

    total_updated = 0
    for owner_id in owners:
        owner_updated = 0
        while True:
            with conn.transaction():
                conn.execute(
                    "SELECT set_config('app.current_user', %s, true)", (owner_id,)
                )
                rows = conn.execute(
                    "SELECT id, data FROM entries "
                    "WHERE language = 'simple'::regconfig AND deleted IS NULL "
                    "ORDER BY created "
                    "LIMIT %s",
                    (BATCH_SIZE,),
                ).fetchall()

            if not rows:
                break

            batch_updated = 0
            for row_id, data in rows:
                if isinstance(data, str):
                    data = json.loads(data)
                text = _compose_text(data)
                lang = resolve_language(text_for_detection=text)
                if lang != "simple":
                    if args.dry_run:
                        desc = (data.get("description") or "")[:60]
                        print(f"  [dry-run] {row_id[:8]}... -> {lang:12s} | {desc}")
                    else:
                        with conn.transaction():
                            conn.execute(
                                "SELECT set_config('app.current_user', %s, true)",
                                (owner_id,),
                            )
                            conn.execute(
                                "UPDATE entries SET language = %s::regconfig WHERE id = %s",
                                (lang, row_id),
                            )
                    batch_updated += 1
            owner_updated += batch_updated
            if batch_updated == 0:
                break

        if owner_updated:
            print(f"  {owner_id}: updated {owner_updated} entries")
        total_updated += owner_updated

    conn.close()
    action = "would update" if args.dry_run else "updated"
    print(f"\nDone. {action} {total_updated} entries total.")


if __name__ == "__main__":
    main()
