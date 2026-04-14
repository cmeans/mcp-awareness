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

            # Create policies — entries gets the _system-schema read carve-out
            # added in migration n9i0j1k2l3m4 so non-privileged owners can see
            # built-in schemas. The WITH CHECK clause is explicit because
            # permissive policies combine with OR, and without it the USING
            # clause would leak into the write path (PR #287 Round-3 finding).
            # Other tables keep strict owner isolation.
            if table == "entries":
                cur.execute(f"""
                    CREATE POLICY owner_isolation ON {table}
                        USING (
                            owner_id = current_setting('app.current_user', true)
                            OR (owner_id = '_system' AND type = 'schema')
                        )
                        WITH CHECK (owner_id = current_setting('app.current_user', true))
                """)
            else:
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
            expires=None,
            data={"description": "test entry for actions"},
        )
        rls_store.add("alice", entry)
        rls_store.log_action("alice", entry.id, action="tested", detail="rls test")

        alice_actions = rls_store.get_actions("alice", entry_id=entry.id)
        bob_actions = rls_store.get_actions("bob", entry_id=entry.id)
        assert len(alice_actions) >= 1
        assert len(bob_actions) == 0


class TestRLSSystemSchemaFallback:
    """RLS carve-out for `_system`-owned schema reads (migration n9i0j1k2l3m4).

    Regression coverage for the PR #287 Round-2 blocker: the strict
    `owner_id = current_user` USING clause made `_system` schemas invisible
    to every non-superuser owner, breaking the CLI bootstrap + find_schema
    fallback in production. These tests run under FORCE ROW LEVEL SECURITY,
    which simulates the production non-superuser role.
    """

    def test_system_schema_visible_to_any_owner(self, rls_store: PostgresStore) -> None:
        """A `_system`-owned schema row is readable by `alice` via find_schema."""
        from mcp_awareness.schema import Entry

        schema_entry = Entry(
            id=make_id(),
            type=EntryType.SCHEMA,
            source="system-bootstrap",
            tags=[],
            created=now_utc(),
            expires=None,
            data={
                "family": "schema:shared-thing",
                "version": "1.0.0",
                "schema": {"type": "object"},
                "description": "shared",
            },
            logical_key="schema:shared-thing:1.0.0",
        )
        rls_store.add("_system", schema_entry)

        found = rls_store.find_schema("alice", "schema:shared-thing:1.0.0")
        assert found is not None
        assert found.id == schema_entry.id
        assert found.data["family"] == "schema:shared-thing"

    def test_caller_schema_wins_over_system(self, rls_store: PostgresStore) -> None:
        """If alice has her own copy, find_schema returns that instead of _system's."""
        from mcp_awareness.schema import Entry

        system_entry = Entry(
            id=make_id(),
            type=EntryType.SCHEMA,
            source="system-bootstrap",
            tags=[],
            created=now_utc(),
            expires=None,
            data={"family": "schema:override", "version": "1.0.0", "schema": {"type": "object"}},
            logical_key="schema:override:1.0.0",
        )
        rls_store.add("_system", system_entry)

        alice_entry = Entry(
            id=make_id(),
            type=EntryType.SCHEMA,
            source="alice-source",
            tags=[],
            created=now_utc(),
            expires=None,
            data={"family": "schema:override", "version": "1.0.0", "schema": {"type": "string"}},
            logical_key="schema:override:1.0.0",
        )
        rls_store.add("alice", alice_entry)

        found = rls_store.find_schema("alice", "schema:override:1.0.0")
        assert found is not None
        assert found.id == alice_entry.id

    def test_system_non_schema_rows_remain_invisible(self, rls_store: PostgresStore) -> None:
        """The carve-out is narrow: only `type = 'schema'`. Other _system rows stay hidden."""
        from mcp_awareness.schema import Entry

        note_entry = Entry(
            id=make_id(),
            type=EntryType.NOTE,
            source="sys-note",
            tags=["rls-sys-note"],
            created=now_utc(),
            expires=None,
            data={"description": "system-only"},
        )
        rls_store.add("_system", note_entry)

        alice_view = rls_store.get_entries("alice", tags=["rls-sys-note"])
        assert alice_view == []

    def test_nonsuperuser_cannot_insert_as_system(self, rls_store: PostgresStore) -> None:
        """Non-privileged owners must not be able to write to `_system`.

        This exercises the WITH CHECK clause against a real non-superuser role
        — the production deployment target. Container superusers have
        BYPASSRLS implicitly, so the raw INSERT against the default role
        would silently succeed and leave the policy untested. We create a
        NOSUPERUSER NOBYPASSRLS role, GRANT only what's needed, then
        ``SET LOCAL ROLE`` onto it for the duration of the test transaction.

        Regression for PR #287 Round-3: the original migration omitted the
        explicit WITH CHECK, so the `_system`-schema carve-out in USING
        leaked into INSERT/UPDATE via the FOR ALL permissive policy.
        """
        from mcp_awareness.schema import Entry

        entry = Entry(
            id=make_id(),
            type=EntryType.SCHEMA,
            source="impostor",
            tags=[],
            created=now_utc(),
            expires=None,
            data={"family": "schema:pwned", "version": "1.0.0", "schema": {"type": "object"}},
            logical_key="schema:pwned:1.0.0",
        )

        # Provision the non-superuser role once per test (idempotent). Use a
        # separate connection so the CREATE/GRANT commits regardless of the
        # main test transaction's outcome.
        with rls_store._pool.connection() as conn, conn.cursor() as cur:
            conn.autocommit = True
            cur.execute(
                "DO $$ BEGIN "
                "IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='rls_prod_sim') THEN "
                "  CREATE ROLE rls_prod_sim NOSUPERUSER NOBYPASSRLS NOINHERIT; "
                "END IF; END $$"
            )
            cur.execute("GRANT USAGE ON SCHEMA public TO rls_prod_sim")
            cur.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON entries TO rls_prod_sim")

        # Now run the actual test inside a transaction as the simulated prod role.
        with (
            pytest.raises(psycopg.errors.InsufficientPrivilege),
            rls_store._pool.connection() as conn,
            conn.transaction(),
            conn.cursor() as cur,
        ):
            cur.execute("SET LOCAL ROLE rls_prod_sim")
            cur.execute("SELECT set_config('app.current_user', 'alice', true)")
            cur.execute(
                "INSERT INTO entries (id, owner_id, type, source, created, tags, data,"
                " logical_key, language) VALUES (%s, '_system', 'schema', %s, now(), '[]',"
                " %s::jsonb, %s, 'english')",
                (entry.id, entry.source, '{"family": "schema:pwned"}', entry.logical_key),
            )

    def test_nonsuperuser_cannot_update_system_schema(self, rls_store: PostgresStore) -> None:
        """Same WITH CHECK guard — an existing `_system` schema row cannot be
        tampered with by a non-privileged owner via UPDATE."""
        from mcp_awareness.schema import Entry

        seed = Entry(
            id=make_id(),
            type=EntryType.SCHEMA,
            source="system-bootstrap",
            tags=[],
            created=now_utc(),
            expires=None,
            data={"family": "schema:readonly", "version": "1.0.0", "schema": {"type": "object"}},
            logical_key="schema:readonly:1.0.0",
        )
        rls_store.add("_system", seed)

        with rls_store._pool.connection() as conn, conn.cursor() as cur:
            conn.autocommit = True
            cur.execute(
                "DO $$ BEGIN "
                "IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='rls_prod_sim') THEN "
                "  CREATE ROLE rls_prod_sim NOSUPERUSER NOBYPASSRLS NOINHERIT; "
                "END IF; END $$"
            )
            cur.execute("GRANT USAGE ON SCHEMA public TO rls_prod_sim")
            cur.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON entries TO rls_prod_sim")

        with (
            pytest.raises(psycopg.errors.InsufficientPrivilege),
            rls_store._pool.connection() as conn,
            conn.transaction(),
            conn.cursor() as cur,
        ):
            cur.execute("SET LOCAL ROLE rls_prod_sim")
            cur.execute("SELECT set_config('app.current_user', 'alice', true)")
            cur.execute(
                "UPDATE entries SET data = data || '{\"tampered\": true}'::jsonb"
                " WHERE owner_id = '_system' AND type = 'schema'"
            )
