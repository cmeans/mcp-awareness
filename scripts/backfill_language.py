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


def _compose_text(entry_type: str, data: dict) -> str:
    """Build detection text matching what the write tool would have used."""
    from mcp_awareness.language import compose_detection_text

    return compose_detection_text(entry_type, data)


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

    with psycopg.connect(dsn) as conn:
        # Get all distinct owner_ids — this query bypasses RLS because it's
        # a superuser/table-owner query on the owner_id column itself.
        # If RLS blocks even this, fall back to the default owner.
        with conn.transaction():
            conn.execute("SELECT set_config('app.current_user', 'system', true)")
            owners = [
                row[0] for row in conn.execute("SELECT DISTINCT owner_id FROM entries").fetchall()
            ]

        if not owners:
            # RLS blocked even the owner list — try env or positional arg
            fallback = os.environ.get("AWARENESS_DEFAULT_OWNER", "system")
            owners = [fallback]
            print(f"Could not query owner_ids (RLS). Trying default: {owners[0]}")

        # Allow explicit owner override via --owner
        if args.owner:
            owners = [args.owner]
            print(f"Using explicit owner: {args.owner}")

        print(f"Found {len(owners)} owner(s): {owners}")

        total_updated = 0
        for owner_id in owners:
            owner_updated = 0
            offset = 0
            while True:
                with conn.transaction():
                    conn.execute("SELECT set_config('app.current_user', %s, true)", (owner_id,))
                    rows = conn.execute(
                        "SELECT id, type, data FROM entries "
                        "WHERE language = 'simple'::regconfig AND deleted IS NULL "
                        "ORDER BY created "
                        "LIMIT %s OFFSET %s",
                        (BATCH_SIZE, offset),
                    ).fetchall()

                if not rows:
                    break

                batch_updated = 0
                for row_id, entry_type, data in rows:
                    if isinstance(data, str):
                        data = json.loads(data)
                    text = _compose_text(entry_type, data)
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
                if args.dry_run:
                    # In dry-run, entries aren't updated so the same batch would
                    # re-fetch. Advance offset instead.
                    offset += BATCH_SIZE
                elif batch_updated == 0:
                    # All entries in this batch genuinely detected as 'simple' —
                    # stop to avoid infinite loop.
                    break

            if owner_updated:
                print(f"  {owner_id}: updated {owner_updated} entries")
            total_updated += owner_updated
    action = "would update" if args.dry_run else "updated"
    print(f"\nDone. {action} {total_updated} entries total.")


if __name__ == "__main__":
    main()
