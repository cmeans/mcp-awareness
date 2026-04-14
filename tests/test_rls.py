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

**Guardrail — all RLS tests must run as a non-superuser role.** Superuser
roles implicitly bypass RLS regardless of ``FORCE ROW LEVEL SECURITY``, so a
passing RLS test under the testcontainers default role proves nothing about
the policy. The ``rls_store`` fixture below enforces this by monkey-patching
``PostgresStore._set_rls_context`` to issue ``SET LOCAL ROLE rls_test_role``
before every transaction, so every store API call exercises the policy the
way production does. Tests that need direct-SQL negative cases (e.g.
``test_nonsuperuser_cannot_insert_as_system``) use the same role via an
explicit ``SET LOCAL ROLE`` in their own cursor block.

See issue #289 and the PR #287 QA rounds for the process rationale: rounds
2 and 3 both caught RLS defects that CI reported green because the fixture
ran as superuser. Do not add a new RLS test that skips the role switch.
"""

from __future__ import annotations

from typing import Any

import psycopg
import pytest

from mcp_awareness.postgres_store import PostgresStore
from mcp_awareness.schema import EntryType, make_id, now_utc

RLS_TEST_ROLE = "rls_test_role"
_RLS_TABLES = ("entries", "reads", "actions", "embeddings")
_RLS_SEQUENCES = ("reads_id_seq", "actions_id_seq", "embeddings_id_seq")


def _provision_rls_role(conn: psycopg.Connection) -> None:
    """Idempotently create the non-superuser test role and GRANT the minimum
    privileges needed for every store call path (including SERIAL sequence
    nextval on reads/actions/embeddings)."""
    with conn.cursor() as cur:
        cur.execute(
            "DO $$ BEGIN "
            f"IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='{RLS_TEST_ROLE}') THEN "
            f"  CREATE ROLE {RLS_TEST_ROLE} NOSUPERUSER NOBYPASSRLS NOINHERIT; "
            "END IF; END $$"
        )
        cur.execute(f"GRANT USAGE ON SCHEMA public TO {RLS_TEST_ROLE}")
        for table in _RLS_TABLES:
            cur.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO {RLS_TEST_ROLE}")
        for seq in _RLS_SEQUENCES:
            cur.execute(f"GRANT USAGE, SELECT ON SEQUENCE {seq} TO {RLS_TEST_ROLE}")


@pytest.fixture
def rls_store(pg_dsn: str, monkeypatch: pytest.MonkeyPatch) -> PostgresStore:
    """PostgresStore under RLS, with every store API call rerouted through
    ``rls_test_role`` (NOSUPERUSER NOBYPASSRLS) for the duration of each
    transaction. This ensures the policy is actually enforced — the default
    testcontainers role is a superuser and would silently bypass RLS.

    The monkey-patch of ``_set_rls_context`` issues ``SET LOCAL ROLE`` +
    ``set_config('app.current_user', …)`` together; both are transaction-
    scoped and revert when the store's ``conn.transaction()`` block exits,
    so the pool connection returns to the pool as the superuser it was
    checked out as.
    """
    store = PostgresStore(pg_dsn)

    with psycopg.connect(pg_dsn, autocommit=True) as conn:
        _provision_rls_role(conn)
        with conn.cursor() as cur:
            for table in _RLS_TABLES:
                cur.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
                cur.execute(f"DROP POLICY IF EXISTS owner_isolation ON {table}")
                cur.execute(f"DROP POLICY IF EXISTS owner_insert ON {table}")

                # `entries` gets the _system-schema read carve-out added in
                # migration n9i0j1k2l3m4 with an explicit strict WITH CHECK
                # so the carve-out does not leak into INSERT/UPDATE (PR #287
                # Round-3 finding). Other tables keep strict owner isolation.
                if table == "entries":
                    cur.execute("""
                        CREATE POLICY owner_isolation ON entries
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

                # Force RLS so the table owner also gets the policy applied —
                # the BYPASSRLS attribute on superusers still wins, which is
                # precisely why we switch to rls_test_role below.
                cur.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")

    def _set_rls_ctx_nonsuper(cur: psycopg.Cursor[Any], owner_id: str) -> None:
        cur.execute(f"SET LOCAL ROLE {RLS_TEST_ROLE}")
        cur.execute("SELECT set_config('app.current_user', %s, true)", (owner_id,))

    monkeypatch.setattr(PostgresStore, "_set_rls_context", staticmethod(_set_rls_ctx_nonsuper))

    yield store

    # Cleanup: disable RLS and drop policies (back under superuser).
    with psycopg.connect(pg_dsn, autocommit=True) as conn, conn.cursor() as cur:
        for table in _RLS_TABLES:
            cur.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
            cur.execute(f"DROP POLICY IF EXISTS owner_insert ON {table}")
            cur.execute(f"DROP POLICY IF EXISTS owner_isolation ON {table}")
            cur.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
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

        This exercises the WITH CHECK clause against the fixture's
        ``rls_test_role`` (NOSUPERUSER NOBYPASSRLS). Container superusers
        have BYPASSRLS implicitly, so the raw INSERT against the default
        role would silently succeed and leave the policy untested.

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

        # rls_test_role is already provisioned + GRANTed by the rls_store fixture.
        with (
            pytest.raises(psycopg.errors.InsufficientPrivilege),
            rls_store._pool.connection() as conn,
            conn.transaction(),
            conn.cursor() as cur,
        ):
            cur.execute(f"SET LOCAL ROLE {RLS_TEST_ROLE}")
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

        with (
            pytest.raises(psycopg.errors.InsufficientPrivilege),
            rls_store._pool.connection() as conn,
            conn.transaction(),
            conn.cursor() as cur,
        ):
            cur.execute(f"SET LOCAL ROLE {RLS_TEST_ROLE}")
            cur.execute("SELECT set_config('app.current_user', 'alice', true)")
            cur.execute(
                "UPDATE entries SET data = data || '{\"tampered\": true}'::jsonb"
                " WHERE owner_id = '_system' AND type = 'schema'"
            )


class TestRLSFixtureGuardrail:
    """Meta-tests: prove the `rls_store` fixture is not tautological.

    If either of these fails it means the fixture has silently reverted
    to running as a superuser (bypassing RLS), which would invalidate
    every other test in this file. Keep both — they cover complementary
    failure modes.
    """

    def test_fixture_switches_to_nonsuperuser_role(self, rls_store: PostgresStore) -> None:
        """``_set_rls_context`` must switch the transaction's current_user to
        ``rls_test_role`` (NOSUPERUSER NOBYPASSRLS). If the fixture forgets
        to monkey-patch, store calls run as the container superuser and RLS
        is bypassed regardless of FORCE ROW LEVEL SECURITY."""
        with (
            rls_store._pool.connection() as conn,
            conn.transaction(),
            conn.cursor() as cur,
        ):
            PostgresStore._set_rls_context(cur, "alice")
            cur.execute("SELECT current_user AS u")
            row = cur.fetchone()
            assert row is not None
            assert row["u"] == RLS_TEST_ROLE, (
                f"Expected current_user to be {RLS_TEST_ROLE!r} after "
                f"_set_rls_context; got {row['u']!r}. The rls_store fixture "
                "is not monkey-patching _set_rls_context to SET LOCAL ROLE — "
                "every other RLS test in this file is tautological."
            )

    def test_rls_fixture_actually_enforces_policy(
        self, rls_store: PostgresStore, pg_dsn: str
    ) -> None:
        """Insert bob's entry, then run a raw SELECT as alice (`SET LOCAL ROLE`
        + ``set_config``) without any ``owner_id = %s`` filter. Under the real
        ``owner_isolation`` policy the SELECT returns zero rows; under a
        temporarily-relaxed ``USING (true)`` policy it returns bob's row.

        The raw SELECT is deliberately not via ``get_entries``: that SQL
        already hard-codes ``WHERE owner_id = %s``, which would shield the
        test from any RLS misbehavior and make it tautological. We want to
        prove the policy itself is load-bearing, not just the application
        filter.
        """
        from mcp_awareness.schema import Entry

        entry = Entry(
            id=make_id(),
            type=EntryType.NOTE,
            source="meta-test",
            tags=["meta-rls"],
            created=now_utc(),
            expires=None,
            data={"description": "bob's note — only visible to bob under policy"},
        )
        rls_store.add("bob", entry)

        def _alice_raw_select() -> int:
            with (
                rls_store._pool.connection() as conn,
                conn.transaction(),
                conn.cursor() as cur,
            ):
                cur.execute(f"SET LOCAL ROLE {RLS_TEST_ROLE}")
                cur.execute("SELECT set_config('app.current_user', 'alice', true)")
                cur.execute(
                    "SELECT count(*) AS n FROM entries WHERE tags @> %s::jsonb",
                    ('["meta-rls"]',),
                )
                row = cur.fetchone()
                assert row is not None
                return int(row["n"])

        # Under the real owner_isolation policy, alice sees zero rows.
        assert _alice_raw_select() == 0

        # Relax the policy so USING returns true regardless of owner. If the
        # fixture were silently bypassing RLS, behavior wouldn't change; if
        # the fixture is correctly exercising RLS, alice now sees bob's row —
        # proving the assertion above was load-bearing.
        with psycopg.connect(pg_dsn, autocommit=True) as conn, conn.cursor() as cur:
            cur.execute("DROP POLICY owner_isolation ON entries")
            cur.execute("""
                CREATE POLICY owner_isolation ON entries
                    USING (true)
                    WITH CHECK (owner_id = current_setting('app.current_user', true))
            """)
        try:
            assert _alice_raw_select() == 1
        finally:
            with psycopg.connect(pg_dsn, autocommit=True) as conn, conn.cursor() as cur:
                cur.execute("DROP POLICY owner_isolation ON entries")
                cur.execute("""
                    CREATE POLICY owner_isolation ON entries
                        USING (
                            owner_id = current_setting('app.current_user', true)
                            OR (owner_id = '_system' AND type = 'schema')
                        )
                        WITH CHECK (owner_id = current_setting('app.current_user', true))
                """)
