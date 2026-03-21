#!/usr/bin/env python3
"""Migrate data from SQLite to PostgreSQL.

Usage:
    python examples/migrate_sqlite_to_postgres.py \
        --sqlite ~/awareness/awareness.db \
        --postgres postgresql://awareness:awareness-dev@localhost:5432/awareness

Copies all active entries from SQLite to Postgres. Skips soft-deleted entries.
Safe to run multiple times — uses INSERT ... ON CONFLICT DO NOTHING.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Add src to path for direct execution
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from mcp_awareness.postgres_store import PostgresStore
from mcp_awareness.store import SQLiteStore


def migrate(sqlite_path: str, postgres_dsn: str) -> None:
    print(f"Source: {sqlite_path}")
    print(f"Target: {postgres_dsn.split('@')[1] if '@' in postgres_dsn else postgres_dsn}")

    sqlite_store = SQLiteStore(sqlite_path)
    pg_store = PostgresStore(postgres_dsn)

    # Get all active entries from SQLite
    entries = sqlite_store._query_entries()
    print(f"Found {len(entries)} active entries in SQLite")

    migrated = 0
    skipped = 0
    for entry in entries:
        try:
            with pg_store._conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO entries
                       (id, type, source, created, updated, expires, deleted, tags, data)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb)
                       ON CONFLICT (id) DO NOTHING""",
                    (
                        entry.id,
                        entry.type.value,
                        entry.source,
                        entry.created,
                        entry.updated,
                        entry.expires,
                        None,
                        json.dumps(entry.tags),
                        json.dumps(entry.data),
                    ),
                )
                if cur.rowcount > 0:
                    migrated += 1
                else:
                    skipped += 1
            pg_store._conn.commit()
        except Exception as e:
            pg_store._conn.rollback()
            print(f"  ERROR migrating {entry.id}: {e}")

    print(f"Migrated: {migrated}")
    print(f"Skipped (already exists): {skipped}")

    # Verify
    pg_stats = pg_store.get_stats()
    print(f"\nPostgres store now has {pg_stats['total']} entries")
    for entry_type, count in pg_stats["entries"].items():
        if count > 0:
            print(f"  {entry_type}: {count}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Migrate SQLite data to PostgreSQL")
    parser.add_argument(
        "--sqlite",
        default="~/awareness/awareness.db",
        help="Path to SQLite database",
    )
    parser.add_argument(
        "--postgres",
        default="postgresql://awareness:awareness-dev@localhost:5432/awareness",
        help="PostgreSQL connection string",
    )
    args = parser.parse_args()
    migrate(str(Path(args.sqlite).expanduser()), args.postgres)


if __name__ == "__main__":
    main()
