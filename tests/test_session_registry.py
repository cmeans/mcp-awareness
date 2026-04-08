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
