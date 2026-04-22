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

"""RLS harness R2: background-thread + connection-pool edge-case coverage.

Complements ``tests/test_rls.py`` (R1 request-path coverage) and
``tests/test_rls_migration_safety.py`` (R3 schema-evolution coverage).
Closes R2 of #359 (#362).

## What these tests catch

The cross-tenant defenses in this codebase are **layered**:

1. Every SQL file is owner-scoped via an explicit ``WHERE owner_id = %s``
   parameter — caller passes the expected owner, SQL enforces it.
2. ``PostgresStore._set_rls_context`` sets ``app.current_user`` on the
   transaction so Postgres RLS policies can enforce isolation independently.
3. The pool's default role in production has ``BYPASSRLS`` (and the test
   fixture monkey-patches ``_set_rls_context`` to re-enter as a
   ``NOBYPASSRLS`` role).

Because of the redundancy, **the tests here catch regressions that drop
*all* defenses simultaneously**, not any single layer in isolation. E.g.,
a future refactor that removes the ``WHERE owner_id = %s`` filter from
``cleanup_expired.sql`` **and** forgets ``_set_rls_context`` would surface
in ``TestRLSBackgroundCleanup``; a failure mode that keeps the SQL filter
intact would not. That's defense-in-depth doing its job — one wrong turn
does not leak — and these tests verify that no call-site makes all the
wrong turns at once, which is the real R2 invariant.

## Execution contexts covered

1. **Background daemon paths.** ``PostgresStore._do_cleanup`` (10 s-debounced
   daemon thread) and ``server._embedding_pool`` (2-worker
   ``ThreadPoolExecutor``) both check out connections from the same pool as
   the request path but run outside the FastMCP request lifecycle. Tests
   exercise both the synchronous call paths and the real threaded paths
   (spawned daemon thread; ``ThreadPoolExecutor.submit``).

2. **Connection-pool behavior.** ``psycopg_pool`` reuses connections across
   transactions, threads, and after exceptions. Two classes of tests:
   - One test (``test_set_config_is_local_…``) directly verifies
     Postgres's ``set_config('app.current_user', …, is_local)`` semantics
     with a raw ``psycopg.connect`` (no pool, no reset callback). This
     isolates the transaction-local-vs-session-scoped behavior from the
     pool's ``RESET ALL`` check-in reset, so it would fail if a future
     bump to psycopg changed that semantic.
   - The remaining pool tests codify the *combined* behavior of Postgres
     + ``psycopg_pool``'s default ``reset`` callback + transaction-scoped
     ``set_config``. They pass under any configuration where the overall
     pool contract (no RLS context leaks across checkouts) is intact, but
     do **not** distinguish which layer delivers that contract — that's
     intentional, because any of those layers regressing alone should not
     cause an observable leak either.

## Audit summary

- **Cleanup thread.** The initial enumeration query
  (``get_cleanup_opted_in_owners``) runs **without** an RLS context — it
  relies on the pool role having ``BYPASSRLS`` (app role in production, the
  default superuser in tests). The per-owner ``DELETE`` in
  ``cleanup_expired`` runs **with** ``_set_rls_context`` AND a
  ``WHERE owner_id = %s`` SQL filter. Cross-tenant safe (two overlapping
  mechanisms).
- **Embedding pool.** ``PostgresStore.upsert_embedding`` is the sole pool
  entry point from ``server._do_embed``. Sets RLS context before the
  INSERT/UPDATE; the SQL also has an explicit ``owner_id`` parameter.
  Cross-tenant safe (two overlapping mechanisms).

Helpers (``RLS_TEST_ROLE``, ``_provision_rls_role``, ``rls_store``) duplicate
those in ``test_rls.py`` by design — keeping R2 single-concern. Factoring
into a shared ``tests/_rls_helpers.py`` is a follow-up refactor (tracked
alongside R1's duplication note and R3's).
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from typing import Any

import psycopg
import pytest

from mcp_awareness.postgres_store import PostgresStore
from mcp_awareness.schema import Entry, EntryType, make_id, now_utc

RLS_TEST_ROLE = "rls_test_role"
_RLS_TABLES = ("entries", "reads", "actions", "embeddings")
_RLS_SEQUENCES = ("reads_id_seq", "actions_id_seq", "embeddings_id_seq")


def _provision_rls_role(conn: psycopg.Connection) -> None:
    """Idempotently create the non-superuser test role and grant the minimum
    privileges for every store call path."""
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
def rls_store(pg_dsn: str, monkeypatch: pytest.MonkeyPatch) -> Any:
    """PostgresStore with every request-path transaction re-routed through
    ``rls_test_role`` (NOSUPERUSER NOBYPASSRLS). Mirror of the fixture in
    ``test_rls.py`` — see that file for the design rationale."""
    store = PostgresStore(pg_dsn)

    with psycopg.connect(pg_dsn, autocommit=True) as conn:
        _provision_rls_role(conn)
        with conn.cursor() as cur:
            for table in _RLS_TABLES:
                cur.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
                cur.execute(f"DROP POLICY IF EXISTS owner_isolation ON {table}")
                cur.execute(f"DROP POLICY IF EXISTS owner_insert ON {table}")
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
                cur.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")

    def _set_rls_ctx_nonsuper(cur: psycopg.Cursor[Any], owner_id: str) -> None:
        cur.execute(f"SET LOCAL ROLE {RLS_TEST_ROLE}")
        cur.execute("SELECT set_config('app.current_user', %s, true)", (owner_id,))

    monkeypatch.setattr(PostgresStore, "_set_rls_context", staticmethod(_set_rls_ctx_nonsuper))

    yield store

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


def _expired_entry(description: str) -> Entry:
    """Build a NOTE entry with an already-elapsed ``expires`` timestamp."""
    return Entry(
        id=make_id(),
        type=EntryType.NOTE,
        source="r2-test",
        tags=["r2-cleanup"],
        created=now_utc() - timedelta(days=2),
        expires=now_utc() - timedelta(days=1),
        data={"description": description},
    )


def _live_entry(description: str) -> Entry:
    """Build a NOTE entry with a future ``expires`` timestamp (not subject to
    cleanup)."""
    return Entry(
        id=make_id(),
        type=EntryType.NOTE,
        source="r2-test",
        tags=["r2-live"],
        created=now_utc(),
        expires=now_utc() + timedelta(days=30),
        data={"description": description},
    )


def _opt_in_auto_cleanup(store: PostgresStore, owner_id: str) -> None:
    store.upsert_preference(
        owner_id,
        key="auto_cleanup",
        scope="global",
        tags=["r2-test"],
        data={"key": "auto_cleanup", "value": "true", "scope": "global"},
    )


class TestRLSBackgroundCleanup:
    """Cleanup thread (``_do_cleanup``) respects tenant isolation.

    The thread's first query (owner enumeration) runs as the pool's default
    role; the per-owner ``DELETE`` runs under the monkey-patched
    ``rls_test_role``. The DELETE is the only path that can touch data, so
    RLS + the ``WHERE owner_id = %s`` filter together enforce isolation.
    """

    def test_cleanup_isolates_expired_deletions_per_owner(self, rls_store: PostgresStore) -> None:
        """Only the opted-in owner's expired entries get deleted; another
        owner's expired entries stay intact even when both coexist in the
        same database."""
        _opt_in_auto_cleanup(rls_store, "alice")
        rls_store.add("alice", _expired_entry("alice-expired"))
        rls_store.add("bob", _expired_entry("bob-expired"))

        rls_store._last_cleanup = 0.0
        rls_store._do_cleanup()

        assert len(rls_store.get_entries("alice", tags=["r2-cleanup"])) == 0
        assert len(rls_store.get_entries("bob", tags=["r2-cleanup"])) == 1

    def test_cleanup_skips_owners_without_preference(self, rls_store: PostgresStore) -> None:
        """An owner who has not opted in keeps their expired entries after a
        cleanup pass — the enumeration query filters to ``auto_cleanup=true``
        preferences only."""
        rls_store.add("alice", _expired_entry("alice-expired-no-opt-in"))

        rls_store._last_cleanup = 0.0
        rls_store._do_cleanup()

        assert len(rls_store.get_entries("alice", tags=["r2-cleanup"])) == 1

    def test_cleanup_preserves_non_expired_entries_for_opted_in_owner(
        self, rls_store: PostgresStore
    ) -> None:
        """Cleanup touches only rows where ``expires <= now`` — future-dated
        or never-expiring entries for opted-in owners are untouched."""
        _opt_in_auto_cleanup(rls_store, "alice")
        rls_store.add("alice", _live_entry("alice-live"))
        rls_store.add("alice", _expired_entry("alice-expired"))

        rls_store._last_cleanup = 0.0
        rls_store._do_cleanup()

        assert len(rls_store.get_entries("alice", tags=["r2-live"])) == 1
        assert len(rls_store.get_entries("alice", tags=["r2-cleanup"])) == 0

    def test_cleanup_expired_background_thread_preserves_isolation(
        self, rls_store: PostgresStore
    ) -> None:
        """When cleanup runs on the spawned daemon thread (not synchronously),
        the per-owner RLS context is still set inside the thread's
        transaction — it does not inherit or contaminate the caller's
        context."""
        _opt_in_auto_cleanup(rls_store, "alice")
        rls_store.add("alice", _expired_entry("alice-thread-expired"))
        rls_store.add("bob", _expired_entry("bob-thread-expired"))

        rls_store._last_cleanup = 0.0
        rls_store._cleanup_expired()
        thread = rls_store._cleanup_thread
        assert thread is not None
        thread.join(timeout=10.0)
        assert not thread.is_alive(), "cleanup thread did not finish in 10s"

        assert len(rls_store.get_entries("alice", tags=["r2-cleanup"])) == 0
        assert len(rls_store.get_entries("bob", tags=["r2-cleanup"])) == 1


class TestRLSBackgroundEmbedding:
    """Embedding pool path (``upsert_embedding``) respects tenant isolation.

    ``server._embedding_pool`` submits ``_do_embed`` — the only database
    write in that path is ``store.upsert_embedding``, which must set RLS
    context before the INSERT.
    """

    def test_upsert_embedding_respects_owner_isolation(self, rls_store: PostgresStore) -> None:
        """An embedding upserted for alice is invisible to bob (embeddings
        table has its own ``owner_isolation`` RLS policy)."""
        alice_entry = Entry(
            id=make_id(),
            type=EntryType.NOTE,
            source="r2-embed",
            tags=["r2-embed"],
            created=now_utc(),
            expires=None,
            data={"description": "alice embedded"},
        )
        rls_store.add("alice", alice_entry)

        rls_store.upsert_embedding(
            "alice",
            alice_entry.id,
            model="test-model",
            dimensions=768,
            text_hash="hash-a",
            embedding=[0.1] * 768,
        )

        missing_for_alice = rls_store.get_entries_without_embeddings(
            "alice", model="test-model", limit=10
        )
        assert all(e.id != alice_entry.id for e in missing_for_alice), (
            "alice's embedding should have been recorded for her own entry."
        )

        missing_for_bob = rls_store.get_entries_without_embeddings(
            "bob", model="test-model", limit=10
        )
        assert alice_entry.id not in [e.id for e in missing_for_bob], (
            "bob must not see alice's entry at all — not as a missing-embedding "
            "candidate, and not via the embedding itself"
        )

    def test_upsert_embedding_from_worker_thread_preserves_isolation(
        self, rls_store: PostgresStore
    ) -> None:
        """When ``upsert_embedding`` runs on a thread-pool worker (as
        ``server._embedding_pool`` invokes it), the worker sets its own RLS
        context inside its transaction — the main thread's context is not
        inherited. This guards against a worker silently writing for the
        wrong owner if Python's ``contextvars`` ever held a stale value."""
        alice_entry = Entry(
            id=make_id(),
            type=EntryType.NOTE,
            source="r2-embed-thread",
            tags=["r2-embed-thread"],
            created=now_utc(),
            expires=None,
            data={"description": "alice embedded from worker"},
        )
        rls_store.add("alice", alice_entry)

        with ThreadPoolExecutor(max_workers=1, thread_name_prefix="r2-embed") as pool:
            future = pool.submit(
                rls_store.upsert_embedding,
                "alice",
                alice_entry.id,
                "test-model",
                768,
                "hash-worker",
                [0.5] * 768,
            )
            future.result(timeout=10.0)

        missing_for_alice = rls_store.get_entries_without_embeddings(
            "alice", model="test-model", limit=10
        )
        assert all(e.id != alice_entry.id for e in missing_for_alice)

        missing_for_bob = rls_store.get_entries_without_embeddings(
            "bob", model="test-model", limit=10
        )
        assert alice_entry.id not in [e.id for e in missing_for_bob]


class TestRLSPoolGuarantees:
    """Connection-pool invariants that RLS-correctness relies on.

    Two flavors of test here. The first two directly probe Postgres's
    ``set_config('app.current_user', …, is_local)`` semantics with a raw
    ``psycopg.connect`` — no pool, no reset callback — so they surface a
    regression in the underlying database or driver behavior independently
    of how ``psycopg_pool`` is configured. The remaining tests codify the
    *combined* pool + Postgres contract that no call-site observes a
    context leak across pool checkouts; they intentionally don't isolate
    which layer delivers that contract (the redundancy is the point).
    """

    def test_set_config_is_local_true_does_not_persist_across_transactions(
        self, pg_dsn: str
    ) -> None:
        """Direct Postgres check: ``set_config(..., true)`` is reverted at
        COMMIT. Uses a raw ``psycopg.connect`` (not ``rls_store._pool``) so
        the assertion is about Postgres itself, not about
        ``psycopg_pool``'s ``RESET ALL`` check-in reset."""
        with psycopg.connect(pg_dsn) as conn:
            with conn.transaction(), conn.cursor() as cur:
                cur.execute("SELECT set_config('app.current_user', 'alice', true)")
            with conn.cursor() as cur:
                cur.execute("SELECT current_setting('app.current_user', true)")
                value = cur.fetchone()[0]
        assert not value, (
            f"set_config(..., true) survived COMMIT: got {value!r}; "
            "Postgres contract is transaction-local"
        )

    def test_set_config_is_local_false_persists_across_transactions(self, pg_dsn: str) -> None:
        """Sanity counter-check: ``set_config(..., false)`` does persist
        across transactions on the same connection. Pairs with the
        previous test to prove the ``is_local=true`` result is produced
        by the flag value, not some ambient reset."""
        with psycopg.connect(pg_dsn) as conn:
            with conn.transaction(), conn.cursor() as cur:
                cur.execute("SELECT set_config('app.current_user', 'alice', false)")
            with conn.cursor() as cur:
                cur.execute("SELECT current_setting('app.current_user', true)")
                value = cur.fetchone()[0]
        assert value == "alice", (
            f"set_config(..., false) did not persist: got {value!r}; "
            "Postgres contract is session-scoped"
        )

    def test_pool_checkout_does_not_see_prior_rls_context(self, rls_store: PostgresStore) -> None:
        """After a store operation returns its connection to the pool,
        a fresh checkout observes no ``app.current_user``. Passes under
        any combination of (transaction-local ``set_config`` AND/OR the
        pool's ``RESET ALL`` check-in reset) — the test verifies the
        *combined* contract, not either piece in isolation."""
        rls_store.add("alice", _live_entry("alice-persist-check"))

        with rls_store._pool.connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT current_setting('app.current_user', true) AS val")
            value = cur.fetchone()["val"]
        assert not value, (
            f"app.current_user leaked across pool checkouts: got {value!r}; expected empty/NULL"
        )

    def test_rls_context_cleared_after_exception_rollback(self, rls_store: PostgresStore) -> None:
        """An exception raised inside a store-style transaction rolls back
        ``set_config`` along with everything else — combined with the
        pool's check-in reset, the connection returns to the pool with no
        RLS residue. Like the previous test, this codifies the *combined*
        contract rather than isolating transaction-local vs.
        pool-reset behavior."""

        class _Sentinel(RuntimeError):
            pass

        with (
            pytest.raises(_Sentinel),
            rls_store._pool.connection() as conn,
            conn.transaction(),
            conn.cursor() as cur,
        ):
            PostgresStore._set_rls_context(cur, "alice")
            cur.execute("SELECT current_setting('app.current_user', true) AS val")
            assert cur.fetchone()["val"] == "alice", (
                "fixture setup failed: RLS context should be observable within the same transaction"
            )
            raise _Sentinel("forcing rollback")

        with rls_store._pool.connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT current_setting('app.current_user', true) AS val")
            value = cur.fetchone()["val"]
        assert not value, f"app.current_user survived a rolled-back transaction: got {value!r}"

    def test_concurrent_owners_do_not_cross_contaminate(self, rls_store: PostgresStore) -> None:
        """Two threads simultaneously calling store methods for different
        owners each land their writes under the correct tenant; neither
        thread sees the other's data once both complete. Covers the joint
        contract that ``psycopg_pool`` hands out distinct connections to
        concurrent callers and that each thread's ``_set_rls_context`` +
        ``WHERE owner_id`` chain routes writes to the expected tenant —
        this test does not by itself prove cross-contamination is
        impossible via ``app.current_user`` alone (the pool's per-thread
        checkout already makes that physically impossible), but it does
        prove the overall call path is free of the cross-tenant bugs
        that matter."""
        alice_entry = Entry(
            id=make_id(),
            type=EntryType.NOTE,
            source="r2-concurrent",
            tags=["r2-concurrent-alice"],
            created=now_utc(),
            expires=None,
            data={"description": "alice concurrent"},
        )
        bob_entry = Entry(
            id=make_id(),
            type=EntryType.NOTE,
            source="r2-concurrent",
            tags=["r2-concurrent-bob"],
            created=now_utc(),
            expires=None,
            data={"description": "bob concurrent"},
        )

        start = threading.Barrier(2)

        def _insert(owner_id: str, entry: Entry) -> None:
            start.wait(timeout=5.0)
            rls_store.add(owner_id, entry)

        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="r2-concurrent") as pool:
            fa = pool.submit(_insert, "alice", alice_entry)
            fb = pool.submit(_insert, "bob", bob_entry)
            fa.result(timeout=10.0)
            fb.result(timeout=10.0)

        alice_rows = rls_store.get_entries("alice", tags=["r2-concurrent-alice"])
        bob_rows = rls_store.get_entries("bob", tags=["r2-concurrent-bob"])
        assert [e.id for e in alice_rows] == [alice_entry.id]
        assert [e.id for e in bob_rows] == [bob_entry.id]

        alice_sees_bob = rls_store.get_entries("alice", tags=["r2-concurrent-bob"])
        bob_sees_alice = rls_store.get_entries("bob", tags=["r2-concurrent-alice"])
        assert alice_sees_bob == []
        assert bob_sees_alice == []
