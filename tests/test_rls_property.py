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

"""RLS harness R4: property-based fuzz over cross-tenant query isolation.

Complements the example-based tests in ``tests/test_rls.py`` (R1),
``tests/test_rls_background.py`` (R2), and
``tests/test_rls_migration_safety.py`` (R3).
Closes R4 of #359 (#364).

## What these tests prove

Hypothesis generates (owner_a, owner_b, tags_a, tags_b) tuples where
owner_a != owner_b, then inserts entries for each owner and asserts
the cross-tenant invariant:

    For every pair of distinct owners, no query from owner_b can ever
    observe an entry owned by owner_a — across ``get_entries``,
    ``get_tags``, and ``get_sources``.

Fuzz-space intentionally modest: alphanumeric + dash owner IDs,
witness tags unique per example. The payload variety is not the
point — the point is generating random *pairs* of owners and random
witness tags to catch edge cases an enumeration-based test wouldn't
produce (e.g., owner IDs that share a prefix, tag names with
leading digits, owner IDs equal to a reserved-looking literal).

## Scope and limits

- ``max_examples=50`` per property: enough to exercise shrinking on a
  failure without blowing the suite runtime past ~10 s for this file.
- ``_system`` is explicitly filtered out — it is the shared-schema
  namespace with a dedicated carve-out, covered by
  ``TestRLSSystemSchemaFallback`` in ``tests/test_rls.py``.
- Entry payloads are held constant (trivial ``description`` only).
  Entry-content fuzzing is out of scope for R4 — a separate tracker
  can cover payload-shape property tests if desired.
- ``semantic_search`` fuzz is deliberately deferred: it requires the
  embedding provider to be live, which would make the test
  environment-dependent. Covered by example-based tests already.

## Meta-verification (what these tests catch when both defenses drop)

A single-layer regression does NOT fail these tests — that's defense
in depth working as designed. Patching **both** layers reproduces a
hypothesis failure with shrinking. The repro procedure documents the
aggregate contract rather than a single-layer guarantee:

1. Drop ``WHERE owner_id = %s`` from ``src/mcp_awareness/sql/get_tags.sql``.
2. Weaken the ``entries``-table RLS policy in ``rls_store`` to ``USING (true)``.
3. ``rm -rf .hypothesis`` (hypothesis caches failing examples and
   replays them; clearing the cache forces fresh generation).
4. Re-run ``pytest tests/test_rls_property.py::test_get_tags_cross_tenant_isolation``.

Expected: ``AssertionError: cross-tenant leak: owner_b(...) saw
owner_a's witness tag ...`` with a shrunken minimal counter-example
like ``payload=('0', '00', 'r4-a-0000', 'r4-b-0000', 1, 0)``.
Revert both layers + clear the cache again → all 3 tests pass.

Single-layer meta-verifications (dropping only ``_set_rls_context``,
only the SQL filter, etc.) are covered by example-based tests in
R1/R2/R3; this file intentionally codifies the aggregate contract.

Helpers (``RLS_TEST_ROLE``, ``_provision_rls_role``, ``rls_store``)
duplicate those in ``test_rls.py`` by design — keeping R4 single
concern. Factoring into a shared ``tests/_rls_helpers.py`` is the
follow-up refactor tracked alongside R1/R2/R3's duplication notes.
"""

from __future__ import annotations

from typing import Any

import psycopg
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

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


# ---- Hypothesis strategies ------------------------------------------------

# Owner IDs: alphanumeric + dash, length 1-32. Excludes "_system" (reserved
# carve-out covered by example-based tests). Narrow character set keeps the
# fuzz focused on the isolation invariant rather than SQL/encoding corners.
_OWNER_ALPHABET = st.characters(
    whitelist_categories=("L", "N"),
    whitelist_characters="-",
)
_owner_id = st.text(alphabet=_OWNER_ALPHABET, min_size=1, max_size=32).filter(
    lambda s: s != "_system"
)

# Witness tags: short ASCII, unique per example via hypothesis's own
# randomness. Prefixed so they are easy to grep for in failure reports.
_tag_alphabet = st.characters(
    whitelist_categories=("L", "N"),
    whitelist_characters="-_",
)
_tag_suffix = st.text(alphabet=_tag_alphabet, min_size=4, max_size=16)


@st.composite
def _owners_with_tags(draw: st.DrawFn) -> tuple[str, str, str, str, int, int]:
    """Draw (owner_a, owner_b, witness_tag_a, witness_tag_b, n_a, n_b).

    - ``owner_a`` and ``owner_b`` are distinct.
    - ``witness_tag_a`` and ``witness_tag_b`` are per-example identifiers so
      entries from earlier hypothesis examples cannot contaminate later
      assertions.
    - ``n_a`` / ``n_b`` are entry counts per owner (at least one on the
      ``a`` side so the cross-tenant check has data to isolate).
    """
    owner_a = draw(_owner_id)
    owner_b = draw(_owner_id.filter(lambda x: x != owner_a))
    witness_a = f"r4-a-{draw(_tag_suffix)}"
    witness_b = f"r4-b-{draw(_tag_suffix)}"
    n_a = draw(st.integers(min_value=1, max_value=3))
    n_b = draw(st.integers(min_value=0, max_value=3))
    return owner_a, owner_b, witness_a, witness_b, n_a, n_b


def _insert_entries(
    store: PostgresStore, owner_id: str, tag: str, count: int, source: str
) -> set[str]:
    """Insert ``count`` entries for ``owner_id`` under ``tag``; return their IDs."""
    ids: set[str] = set()
    for i in range(count):
        entry = Entry(
            id=make_id(),
            type=EntryType.NOTE,
            source=source,
            tags=[tag],
            created=now_utc(),
            expires=None,
            data={"description": f"r4-fuzz entry {i} for {owner_id}"},
        )
        store.add(owner_id, entry)
        ids.add(entry.id)
    return ids


# ---- Property tests -------------------------------------------------------


@given(payload=_owners_with_tags())
@settings(
    max_examples=50,
    deadline=None,
    # The rls_store fixture is function-scoped, which hypothesis warns about
    # because the same store is reused across generated examples. Intentional
    # here: each example uses unique witness tags + unique owner IDs so prior
    # examples cannot contaminate later assertions; the test asserts the
    # isolation property per example on that example's own ids, not on the
    # absolute DB state. Suppressing the health-check keeps the warning out
    # of CI noise while documenting the intent.
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
def test_get_entries_cross_tenant_isolation(
    payload: tuple[str, str, str, str, int, int],
    rls_store: PostgresStore,
) -> None:
    """Across randomly-generated owner pairs, ``get_entries`` from one
    owner never returns entries inserted under the other owner.

    Covers both directions (a→b and b→a) and the owner's own visibility
    guarantee (each owner sees their own witness-tagged entries)."""
    owner_a, owner_b, tag_a, tag_b, n_a, n_b = payload

    a_ids = _insert_entries(rls_store, owner_a, tag_a, n_a, source="r4-fuzz-a")
    b_ids = _insert_entries(rls_store, owner_b, tag_b, n_b, source="r4-fuzz-b")

    # Owner A sees at least their own tagged entries. ``>=`` instead of
    # ``==`` because hypothesis shrinking can replay the same (owner, tag)
    # pair across examples and accumulate ids under that key — not a bug,
    # and outside the isolation invariant we actually care about.
    a_sees_a_ids = {e.id for e in rls_store.get_entries(owner_a, tags=[tag_a])}
    assert a_ids <= a_sees_a_ids, (
        f"owner_a({owner_a!r}) lost visibility of their own entries under tag={tag_a!r}; "
        f"missing={a_ids - a_sees_a_ids}"
    )

    # Core invariant: owner_b's query never returns any id owned by owner_a.
    b_sees_a_tag_ids = {e.id for e in rls_store.get_entries(owner_b, tags=[tag_a])}
    leak_a_to_b = a_ids & b_sees_a_tag_ids
    assert not leak_a_to_b, (
        f"cross-tenant leak: owner_b({owner_b!r}) saw owner_a({owner_a!r})'s ids "
        f"{leak_a_to_b} when querying tag={tag_a!r}"
    )

    if n_b > 0:
        # Symmetric checks (only meaningful when B actually inserted entries).
        b_sees_b_ids = {e.id for e in rls_store.get_entries(owner_b, tags=[tag_b])}
        assert b_ids <= b_sees_b_ids, (
            f"owner_b({owner_b!r}) lost visibility of their own entries under tag={tag_b!r}"
        )

        a_sees_b_tag_ids = {e.id for e in rls_store.get_entries(owner_a, tags=[tag_b])}
        leak_b_to_a = b_ids & a_sees_b_tag_ids
        assert not leak_b_to_a, (
            f"cross-tenant leak: owner_a({owner_a!r}) saw owner_b({owner_b!r})'s ids "
            f"{leak_b_to_a} when querying tag={tag_b!r}"
        )


@given(payload=_owners_with_tags())
@settings(
    max_examples=50,
    deadline=None,
    # The rls_store fixture is function-scoped, which hypothesis warns about
    # because the same store is reused across generated examples. Intentional
    # here: each example uses unique witness tags + unique owner IDs so prior
    # examples cannot contaminate later assertions; the test asserts the
    # isolation property per example on that example's own ids, not on the
    # absolute DB state. Suppressing the health-check keeps the warning out
    # of CI noise while documenting the intent.
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
def test_get_tags_cross_tenant_isolation(
    payload: tuple[str, str, str, str, int, int],
    rls_store: PostgresStore,
) -> None:
    """``get_tags`` for one owner never exposes tags that exist only on
    the other owner's entries."""
    owner_a, owner_b, tag_a, tag_b, n_a, n_b = payload

    _insert_entries(rls_store, owner_a, tag_a, n_a, source="r4-fuzz-a")
    _insert_entries(rls_store, owner_b, tag_b, n_b, source="r4-fuzz-b")

    # Owner B's tag view never includes A's witness tag.
    b_tags = {t.get("tag") for t in rls_store.get_tags(owner_b)}
    assert tag_a not in b_tags, (
        f"cross-tenant leak: owner_b({owner_b!r}) saw owner_a's witness tag {tag_a!r}"
    )

    if n_b > 0:
        # Symmetric check (only meaningful when B actually has entries).
        a_tags = {t.get("tag") for t in rls_store.get_tags(owner_a)}
        assert tag_b not in a_tags, (
            f"cross-tenant leak: owner_a({owner_a!r}) saw owner_b's witness tag {tag_b!r}"
        )


@given(payload=_owners_with_tags())
@settings(
    max_examples=50,
    deadline=None,
    # The rls_store fixture is function-scoped, which hypothesis warns about
    # because the same store is reused across generated examples. Intentional
    # here: each example uses unique witness tags + unique owner IDs so prior
    # examples cannot contaminate later assertions; the test asserts the
    # isolation property per example on that example's own ids, not on the
    # absolute DB state. Suppressing the health-check keeps the warning out
    # of CI noise while documenting the intent.
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
def test_get_sources_cross_tenant_isolation(
    payload: tuple[str, str, str, str, int, int],
    rls_store: PostgresStore,
) -> None:
    """``get_sources`` for one owner never exposes source values that
    exist only on the other owner's entries."""
    owner_a, owner_b, tag_a, tag_b, n_a, n_b = payload

    # Use per-owner source values derived from the witness tags so they are
    # guaranteed distinct between a and b.
    source_a = f"r4-src-a-{tag_a}"
    source_b = f"r4-src-b-{tag_b}"
    _insert_entries(rls_store, owner_a, tag_a, n_a, source=source_a)
    _insert_entries(rls_store, owner_b, tag_b, n_b, source=source_b)

    b_sources = set(rls_store.get_sources(owner_b))
    assert source_a not in b_sources, (
        f"cross-tenant leak: owner_b({owner_b!r}) saw owner_a's source {source_a!r}"
    )

    if n_b > 0:
        a_sources = set(rls_store.get_sources(owner_a))
        assert source_b not in a_sources, (
            f"cross-tenant leak: owner_a({owner_a!r}) saw owner_b's source {source_b!r}"
        )
