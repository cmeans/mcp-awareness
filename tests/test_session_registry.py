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

"""Tests for MCP session persistence (SessionStore + SessionRegistryMiddleware)."""

from __future__ import annotations

import pytest

from mcp_awareness.session_registry import SessionStore

TEST_OWNER = "test-owner"


@pytest.fixture
def session_store(pg_dsn):
    """Fresh SessionStore for each test."""
    store = SessionStore(pg_dsn, ttl_seconds=300)
    yield store
    # Clean up all sessions after each test
    with store._pool.connection() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM session_redirects")
        cur.execute("DELETE FROM session_registry")
    store.close()


class TestSessionStoreSchema:
    """Tests for schema creation."""

    def test_ensure_schema_idempotent(self, session_store: SessionStore) -> None:
        """Calling ensure_schema twice does not raise."""
        session_store.ensure_schema()
        session_store.ensure_schema()

    def test_tables_exist(self, session_store: SessionStore) -> None:
        """Both tables are created."""
        with session_store._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT tablename FROM pg_tables WHERE tablename IN "
                "('session_registry', 'session_redirects') ORDER BY tablename"
            )
            tables = [row["tablename"] for row in cur.fetchall()]
        assert tables == ["session_redirects", "session_registry"]


class TestSessionStoreRegisterLookup:
    """Tests for register and lookup."""

    def test_register_and_lookup(self, session_store: SessionStore) -> None:
        """Registered session is returned by lookup."""
        session_store.register(
            session_id="sess-1",
            owner_id=TEST_OWNER,
            node="app-a",
            protocol_version="2025-03-26",
            capabilities={"roots": {"listChanged": True}},
            client_info={"name": "claude-desktop", "version": "1.0"},
        )
        result = session_store.lookup("sess-1")
        assert result is not None
        assert result["session_id"] == "sess-1"
        assert result["owner_id"] == TEST_OWNER
        assert result["node"] == "app-a"
        assert result["protocol_version"] == "2025-03-26"
        assert result["capabilities"] == {"roots": {"listChanged": True}}
        assert result["client_info"] == {"name": "claude-desktop", "version": "1.0"}

    def test_lookup_nonexistent(self, session_store: SessionStore) -> None:
        """Lookup returns None for unknown session_id."""
        assert session_store.lookup("does-not-exist") is None

    def test_lookup_expired(self, session_store: SessionStore) -> None:
        """Expired sessions are not returned by lookup."""
        short_store = SessionStore(session_store.dsn, ttl_seconds=0)
        short_store.register(
            session_id="sess-expired",
            owner_id=TEST_OWNER,
            node="app-a",
            protocol_version="2025-03-26",
            capabilities={},
            client_info={},
        )
        assert short_store.lookup("sess-expired") is None
        short_store.close()


class TestSessionStoreTouchInvalidateCount:
    """Tests for touch, invalidate, and count_active."""

    def _register_session(self, store: SessionStore, session_id: str = "sess-1") -> None:
        store.register(
            session_id=session_id,
            owner_id=TEST_OWNER,
            node="app-a",
            protocol_version="2025-03-26",
            capabilities={},
            client_info={},
        )

    def test_touch_extends_expiry(self, session_store: SessionStore) -> None:
        """Touch extends the expires_at timestamp."""
        self._register_session(session_store)
        before = session_store.lookup("sess-1")
        assert before is not None
        original_expires = before["expires_at"]
        session_store.touch("sess-1")
        after = session_store.lookup("sess-1")
        assert after is not None
        assert after["expires_at"] >= original_expires

    def test_touch_nonexistent_is_noop(self, session_store: SessionStore) -> None:
        """Touch on a nonexistent session does not raise."""
        session_store.touch("does-not-exist")

    def test_invalidate(self, session_store: SessionStore) -> None:
        """Invalidated session is no longer returned by lookup."""
        self._register_session(session_store)
        assert session_store.lookup("sess-1") is not None
        session_store.invalidate("sess-1")
        assert session_store.lookup("sess-1") is None

    def test_count_active(self, session_store: SessionStore) -> None:
        """count_active returns the number of non-expired sessions for an owner."""
        assert session_store.count_active(TEST_OWNER) == 0
        self._register_session(session_store, "sess-1")
        self._register_session(session_store, "sess-2")
        assert session_store.count_active(TEST_OWNER) == 2
        session_store.invalidate("sess-1")
        assert session_store.count_active(TEST_OWNER) == 1

    def test_count_active_ignores_other_owners(self, session_store: SessionStore) -> None:
        """count_active only counts sessions for the specified owner."""
        self._register_session(session_store, "sess-1")
        session_store.register(
            session_id="sess-other",
            owner_id="other-owner",
            node="app-a",
            protocol_version="2025-03-26",
            capabilities={},
            client_info={},
        )
        assert session_store.count_active(TEST_OWNER) == 1
        assert session_store.count_active("other-owner") == 1


class TestSessionStoreRedirects:
    """Tests for redirect table and cleanup."""

    def test_add_and_lookup_redirect(self, session_store: SessionStore) -> None:
        """Redirect mapping is returned by redirect_lookup."""
        session_store.add_redirect("old-sess", "new-sess")
        result = session_store.redirect_lookup("old-sess")
        assert result == "new-sess"

    def test_redirect_lookup_nonexistent(self, session_store: SessionStore) -> None:
        """redirect_lookup returns None for unknown old_session_id."""
        assert session_store.redirect_lookup("unknown") is None

    def test_redirect_upsert(self, session_store: SessionStore) -> None:
        """Adding a redirect for the same old_session_id updates the target."""
        session_store.add_redirect("old-sess", "new-sess-1")
        session_store.add_redirect("old-sess", "new-sess-2")
        assert session_store.redirect_lookup("old-sess") == "new-sess-2"

    def test_cleanup_expired_sessions(self, session_store: SessionStore) -> None:
        """cleanup_expired removes expired sessions and redirects."""
        store = SessionStore(session_store.dsn, ttl_seconds=0, redirect_grace_seconds=0)
        store.register(
            session_id="sess-expired",
            owner_id="test-owner",
            node="app-a",
            protocol_version="2025-03-26",
            capabilities={},
            client_info={},
        )
        store.add_redirect("old-redirect", "new-redirect")

        count = store.cleanup_expired()
        assert count >= 2

        with store._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) AS cnt FROM session_registry WHERE session_id = 'sess-expired'"
            )
            assert cur.fetchone()["cnt"] == 0
            cur.execute(
                "SELECT COUNT(*) AS cnt FROM session_redirects"
                " WHERE old_session_id = 'old-redirect'"
            )
            assert cur.fetchone()["cnt"] == 0
        store.close()

    def test_cleanup_debounced(self, session_store: SessionStore) -> None:
        """_schedule_cleanup is debounced — second call within interval is a no-op."""
        session_store._schedule_cleanup()
        first_cleanup_time = session_store._last_cleanup
        session_store._schedule_cleanup()
        assert session_store._last_cleanup == first_cleanup_time
