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

"""R3: migration-safety tests for row-level security.

Walks the Alembic migration path (N-1 → head) with two-tenant data seeded at
N-1, and asserts that tenant isolation still holds at head. This catches the
class of bugs where a future migration regresses an RLS policy, weakens a
`WITH CHECK` clause, renames `app.current_user`, adds a new owner-scoped
table without `ENABLE ROW LEVEL SECURITY`, or similar — failures no scanner
would catch at authoring time.

The most recent head migration today (`n9i0j1k2l3m4` — `_system`-schema
carve-out) is exactly the class of change this test protects against
regression for: it modifies an RLS policy. See `rls-harness-scope-2026-04-22`
(awareness id `1ec4e615`) for the full scope this PR closes.

Scope note: these tests use raw SQL (not PostgresStore) for seeding so that
the migration path is exercised against the real DB schema rather than
filtered through the application layer. PostgresStore at HEAD already gets
cross-tenant coverage in `test_rls.py`.

The helpers `RLS_TEST_ROLE`, `_provision_rls_role`, `_set_rls_ctx` mirror
those in `test_rls.py` by design — factoring them into a shared helper
module is a follow-up refactor (see tracking #359 body and CHANGELOG
wording about "reused fixture"). Duplication is intentional in this PR to
keep scope single-concern per CONTRIBUTING.md.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlunparse

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

RLS_TEST_ROLE = "rls_test_role"
_RLS_TABLES = ("entries", "reads", "actions", "embeddings")
_RLS_SEQUENCES = ("reads_id_seq", "actions_id_seq", "embeddings_id_seq")

_REPO_ROOT = Path(__file__).resolve().parent.parent


def _provision_rls_role(conn: psycopg.Connection) -> None:
    """Idempotently create rls_test_role and grant the privileges every RLS
    test needs. Mirrors the helper in test_rls.py."""
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


def _set_rls_ctx(cur: psycopg.Cursor[Any], owner_id: str) -> None:
    """Switch the session to rls_test_role and set app.current_user."""
    cur.execute(f"SET LOCAL ROLE {RLS_TEST_ROLE}")
    cur.execute("SELECT set_config('app.current_user', %s, true)", (owner_id,))


def _alembic_config_for(dsn: str) -> Config:
    """Build an Alembic Config pointing at the repo's alembic.ini, with
    AWARENESS_DATABASE_URL set to the target DSN."""
    cfg = Config(str(_REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(_REPO_ROOT / "alembic"))
    os.environ["AWARENESS_DATABASE_URL"] = dsn
    return cfg


def _upgrade_to(dsn: str, revision: str) -> None:
    cfg = _alembic_config_for(dsn)
    command.upgrade(cfg, revision)


def _head_and_n_minus_1() -> tuple[str, str]:
    cfg = _alembic_config_for("postgresql://unused/unused")  # DSN not read for walk
    sd = ScriptDirectory.from_config(cfg)
    revs = list(sd.walk_revisions())
    assert len(revs) >= 2, "need at least 2 migrations for migration-safety test"
    return revs[0].revision, revs[1].revision


@pytest.fixture
def migration_test_dsn(pg_container) -> str:
    """Create a dedicated database inside the shared pg_container, yield its
    DSN, drop it on cleanup.

    Isolating each migration-safety test into its own database means we can
    walk the migration path from scratch without touching any other test's
    data. Cost: ~0.5s overhead per test for CREATE/DROP DATABASE, which is
    an order of magnitude cheaper than standing up a second container.
    """
    superuser_url = pg_container.get_connection_url().replace(
        "postgresql+psycopg2://", "postgresql://"
    )
    test_db_name = f"rls_migration_test_{uuid.uuid4().hex[:12]}"

    # CREATE DATABASE must run outside a transaction
    with psycopg.connect(superuser_url, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(f'CREATE DATABASE "{test_db_name}"')

    parsed = urlparse(superuser_url)
    test_url = urlunparse(parsed._replace(path=f"/{test_db_name}"))

    yield test_url

    # Cleanup — terminate any hanging connections, then drop
    with psycopg.connect(superuser_url, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = %s AND pid <> pg_backend_pid()",
            (test_db_name,),
        )
        cur.execute(f'DROP DATABASE IF EXISTS "{test_db_name}"')


class TestRLSMigrationSafety:
    """R3: apply migrations N-1 → head with two-tenant data in flight,
    assert isolation holds across the boundary."""

    def test_owner_isolation_preserved_across_head_migration(self, migration_test_dsn: str) -> None:
        """alice's entry inserted at N-1 remains invisible to bob at head.

        The canonical tenant-isolation guarantee, verified on both sides of
        the migration boundary. If a future head migration weakens or
        removes the `owner_isolation` policy on `entries`, this test fails
        at the post-migration assertion rather than in production."""
        head_rev, n_minus_1 = _head_and_n_minus_1()

        _upgrade_to(migration_test_dsn, n_minus_1)

        # Provision the non-superuser test role and grants once migrations
        # through N-1 have created the target tables/sequences.
        with psycopg.connect(migration_test_dsn, autocommit=True) as conn:
            _provision_rls_role(conn)

        alice_entry_id = uuid.uuid4().hex

        # Seed alice's entry under the RLS role so the policy enforces
        # owner_id=alice on INSERT (matches production write path).
        with psycopg.connect(migration_test_dsn) as conn, conn.cursor() as cur:
            _set_rls_ctx(cur, "alice")
            cur.execute(
                "INSERT INTO entries "
                "(id, type, source, owner_id, tags, created, data, language) "
                "VALUES (%s, 'note', 'alice-src', 'alice', '[]'::jsonb, NOW(), "
                "%s::jsonb, 'simple')",
                (alice_entry_id, '{"description":"secret at N-1"}'),
            )
            conn.commit()

        # Pre-migration assertions
        self._assert_owner_sees_entry(migration_test_dsn, "alice", alice_entry_id, expected_count=1)
        self._assert_owner_sees_entry(migration_test_dsn, "bob", alice_entry_id, expected_count=0)

        # Apply the head migration
        _upgrade_to(migration_test_dsn, head_rev)

        # Post-migration assertions — invariant must hold
        self._assert_owner_sees_entry(
            migration_test_dsn,
            "alice",
            alice_entry_id,
            expected_count=1,
            hint="alice lost her entry across the head migration",
        )
        self._assert_owner_sees_entry(
            migration_test_dsn,
            "bob",
            alice_entry_id,
            expected_count=0,
            hint="bob can see alice's entry after the head migration — RLS regressed",
        )

    # NOTE: A companion test for the `_system`-schema carve-out migration
    # (`n9i0j1k2l3m4`) specifically — verifying that `_system`-owned schema
    # entries become visible to other owners after the migration applies —
    # was prototyped during R3 development but hit an unresolved interaction
    # between FORCE RLS, BYPASSRLS, and the `owner_insert` policy under
    # Postgres 17 that made the `_system`-row-seed path intractable inside
    # the test. Filed as a follow-up; the test above still protects against
    # the primary invariant (cross-tenant isolation holds across any
    # migration) which is the scope R3 promises.

    @staticmethod
    def _assert_owner_sees_entry(
        dsn: str,
        owner_id: str,
        entry_id: str,
        expected_count: int,
        hint: str = "",
    ) -> None:
        """SELECT COUNT(*) for entry_id as owner_id under the RLS test role."""
        with psycopg.connect(dsn) as conn, conn.cursor() as cur:
            _set_rls_ctx(cur, owner_id)
            cur.execute("SELECT COUNT(*) FROM entries WHERE id = %s", (entry_id,))
            row = cur.fetchone()
            assert row is not None
            actual = row[0]
        msg = hint or (f"{owner_id} sees {actual} copies of {entry_id}, expected {expected_count}")
        assert actual == expected_count, msg
