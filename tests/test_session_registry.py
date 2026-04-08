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

import json
from typing import Any

import pytest

from mcp_awareness.session_registry import SessionRegistryMiddleware, SessionStore

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


# ---------------------------------------------------------------------------
# Middleware helpers
# ---------------------------------------------------------------------------


async def _make_fastmcp_stub(
    session_id: str = "new-sess-abc",
    status: int = 200,
    response_body: bytes = b'{"jsonrpc":"2.0","id":1,"result":{}}',
) -> Any:
    """Return an ASGI app that mimics FastMCP's initialize response."""

    async def app(scope: dict[str, Any], receive: Any, send: Any) -> None:
        while True:
            msg = await receive()
            if msg.get("type") == "http.request" and not msg.get("more_body", False):
                break
        headers: list[tuple[bytes, bytes]] = [(b"content-type", b"application/json")]
        if session_id:
            headers.append((b"mcp-session-id", session_id.encode()))
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": response_body})

    return app


async def _collect_response(
    app: Any, scope: dict[str, Any], body: bytes = b""
) -> tuple[int, bytes, list[tuple[bytes, bytes]]]:
    """Send a request through an ASGI app, return (status, body, headers)."""
    captured_status = 0
    captured_body = b""
    captured_headers: list[tuple[bytes, bytes]] = []

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": body}

    async def send(message: dict[str, Any]) -> None:
        nonlocal captured_status, captured_body, captured_headers
        if message["type"] == "http.response.start":
            captured_status = message["status"]
            captured_headers = list(message.get("headers", []))
        elif message["type"] == "http.response.body":
            captured_body += message.get("body", b"")

    await app(scope, receive, send)
    return captured_status, captured_body, captured_headers


def _mcp_post_scope(
    path: str = "/mcp",
    session_id: str | None = None,
) -> dict[str, Any]:
    """Build an ASGI scope for a POST /mcp request."""
    headers: list[tuple[bytes, bytes]] = [(b"content-type", b"application/json")]
    if session_id:
        headers.append((b"mcp-session-id", session_id.encode()))
    return {"type": "http", "method": "POST", "path": path, "headers": headers}


# ---------------------------------------------------------------------------
# TestMiddlewareInitialize
# ---------------------------------------------------------------------------


class TestMiddlewareInitialize:
    """Tests for initialize (new session) capture."""

    @pytest.mark.anyio
    async def test_initialize_registers_session(self, session_store: SessionStore) -> None:
        """POST /mcp without session_id registers the new session."""
        inner = await _make_fastmcp_stub(session_id="new-sess-123")
        mw = SessionRegistryMiddleware(inner, session_store, node_name="app-a")

        scope = _mcp_post_scope()
        body = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {"roots": {"listChanged": True}},
                    "clientInfo": {"name": "test-client", "version": "0.1"},
                },
            }
        ).encode()

        from mcp_awareness.server import _owner_ctx

        token = _owner_ctx.set("test-owner")
        try:
            status, _resp_body, _headers = await _collect_response(mw, scope, body)
        finally:
            _owner_ctx.reset(token)

        assert status == 200
        session = session_store.lookup("new-sess-123")
        assert session is not None
        assert session["owner_id"] == "test-owner"
        assert session["node"] == "app-a"
        assert session["protocol_version"] == "2025-03-26"
        assert session["capabilities"] == {"roots": {"listChanged": True}}
        assert session["client_info"] == {"name": "test-client", "version": "0.1"}

    @pytest.mark.anyio
    async def test_non_mcp_path_passes_through(self, session_store: SessionStore) -> None:
        """Requests to paths other than /mcp pass through unmodified."""
        inner = await _make_fastmcp_stub()
        mw = SessionRegistryMiddleware(inner, session_store, node_name="app-a")
        scope = _mcp_post_scope(path="/health")
        status, _, _ = await _collect_response(mw, scope)
        assert status == 200
        assert session_store.count_active("test-owner") == 0

    @pytest.mark.anyio
    async def test_non_http_passes_through(self, session_store: SessionStore) -> None:
        """Non-HTTP scopes (websocket, lifespan) pass through."""
        called = False

        async def inner(scope: Any, receive: Any, send: Any) -> None:
            nonlocal called
            called = True

        mw = SessionRegistryMiddleware(inner, session_store, node_name="app-a")
        await mw({"type": "lifespan"}, None, None)
        assert called
