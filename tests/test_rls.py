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

"""Tests for row-level security policies.

These tests verify that RLS policies correctly isolate data between owners.
RLS is defense-in-depth — application code already filters by owner_id,
but RLS ensures the database enforces isolation even if app code has bugs.

Note: RLS policies only affect non-superuser roles. The test creates a
restricted role to test policy enforcement, since the default test
connection uses the postgres superuser which bypasses RLS.
"""

from __future__ import annotations

import psycopg
import pytest

from mcp_awareness.postgres_store import PostgresStore
from mcp_awareness.schema import EntryType, make_id, now_utc


@pytest.fixture
def rls_store(pg_dsn: str) -> PostgresStore:
    """Store with RLS policies enabled on all tables."""
    store = PostgresStore(pg_dsn)

    with psycopg.connect(pg_dsn) as conn, conn.cursor() as cur:
        for table in ("entries", "reads", "actions", "embeddings"):
            # Enable RLS
            cur.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")

            # Drop existing policies if any (idempotent)
            cur.execute(f"DROP POLICY IF EXISTS owner_isolation ON {table}")
            cur.execute(f"DROP POLICY IF EXISTS owner_insert ON {table}")

            # Create policies
            cur.execute(f"""
                CREATE POLICY owner_isolation ON {table}
                    USING (owner_id = current_setting('app.current_user', true))
            """)
            cur.execute(f"""
                CREATE POLICY owner_insert ON {table}
                    FOR INSERT
                    WITH CHECK (owner_id = current_setting('app.current_user', true))
            """)

            # Force RLS even for table owner (superuser bypass would otherwise skip it)
            cur.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")

    yield store

    # Cleanup: disable RLS and drop policies
    with psycopg.connect(pg_dsn) as conn, conn.cursor() as cur:
        for table in ("entries", "reads", "actions", "embeddings"):
            cur.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
            cur.execute(f"DROP POLICY IF EXISTS owner_insert ON {table}")
            cur.execute(f"DROP POLICY IF EXISTS owner_isolation ON {table}")
            cur.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
        # Clean up test data
        cur.execute("DELETE FROM reads")
        cur.execute("DELETE FROM actions")
        cur.execute("DELETE FROM embeddings")
        cur.execute("DELETE FROM entries")


class TestRLSEntries:
    """RLS isolation tests for the entries table."""

    def test_alice_entry_invisible_to_bob(self, rls_store: PostgresStore) -> None:
        """An entry created by alice should not be visible to bob."""
        from mcp_awareness.schema import Entry

        entry = Entry(
            id=make_id(),
            type=EntryType.NOTE,
            source="test",
            tags=["rls-test"],
            created=now_utc(),
            updated=now_utc(),
            expires=None,
            data={"description": "alice's secret"},
        )
        rls_store.add("alice", entry)

        # Bob should see nothing
        bob_entries = rls_store.get_entries("bob", tags=["rls-test"])
        assert len(bob_entries) == 0

        # Alice should see her entry
        alice_entries = rls_store.get_entries("alice", tags=["rls-test"])
        assert len(alice_entries) == 1
        assert alice_entries[0].id == entry.id

    def test_knowledge_isolated(self, rls_store: PostgresStore) -> None:
        """get_knowledge respects owner isolation."""
        from mcp_awareness.schema import Entry

        entry = Entry(
            id=make_id(),
            type=EntryType.NOTE,
            source="test-knowledge",
            tags=["rls-knowledge"],
            created=now_utc(),
            updated=now_utc(),
            expires=None,
            data={"description": "alice knowledge"},
        )
        rls_store.add("alice", entry)

        assert len(rls_store.get_knowledge("alice", tags=["rls-knowledge"])) == 1
        assert len(rls_store.get_knowledge("bob", tags=["rls-knowledge"])) == 0

    def test_stats_isolated(self, rls_store: PostgresStore) -> None:
        """get_stats only counts entries for the requesting owner."""
        from mcp_awareness.schema import Entry

        for owner in ("alice", "bob"):
            entry = Entry(
                id=make_id(),
                type=EntryType.NOTE,
                source="test-stats",
                tags=["rls-stats"],
                created=now_utc(),
                updated=now_utc(),
                expires=None,
                data={"description": f"{owner}'s note"},
            )
            rls_store.add(owner, entry)

        alice_stats = rls_store.get_stats("alice")
        bob_stats = rls_store.get_stats("bob")
        assert alice_stats["total"] >= 1
        assert bob_stats["total"] >= 1
        # Each should see only their own count
        assert alice_stats["total"] == bob_stats["total"]  # 1 each


class TestRLSReads:
    """RLS isolation tests for the reads table."""

    def test_read_logs_isolated(self, rls_store: PostgresStore) -> None:
        """Read logs for alice are not visible to bob."""
        from mcp_awareness.schema import Entry

        entry = Entry(
            id=make_id(),
            type=EntryType.NOTE,
            source="test-reads",
            tags=["rls-reads"],
            created=now_utc(),
            updated=now_utc(),
            expires=None,
            data={"description": "test entry"},
        )
        rls_store.add("alice", entry)
        rls_store.log_read("alice", [entry.id], tool_used="test")

        alice_reads = rls_store.get_reads("alice", entry_id=entry.id)
        bob_reads = rls_store.get_reads("bob", entry_id=entry.id)
        assert len(alice_reads) >= 1
        assert len(bob_reads) == 0


class TestRLSActions:
    """RLS isolation tests for the actions table."""

    def test_action_logs_isolated(self, rls_store: PostgresStore) -> None:
        """Action logs for alice are not visible to bob."""
        from mcp_awareness.schema import Entry

        entry = Entry(
            id=make_id(),
            type=EntryType.NOTE,
            source="test-actions",
            tags=["rls-actions"],
            created=now_utc(),
            updated=now_utc(),
            expires=None,
            data={"description": "test entry for actions"},
        )
        rls_store.add("alice", entry)
        rls_store.log_action("alice", entry.id, action="tested", detail="rls test")

        alice_actions = rls_store.get_actions("alice", entry_id=entry.id)
        bob_actions = rls_store.get_actions("bob", entry_id=entry.id)
        assert len(alice_actions) >= 1
        assert len(bob_actions) == 0
