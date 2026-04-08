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

import contextlib
import json
from typing import Any
from unittest.mock import patch

import pytest

from mcp_awareness.session_registry import SessionRegistryMiddleware, SessionStore

TEST_OWNER = "test-owner"


@pytest.fixture
def session_store(pg_dsn):
    """Fresh SessionStore for each test."""
    store = SessionStore(pg_dsn, ttl_seconds=300)
    yield store
    # Clean up if pool is still open (some tests close it to test degradation)
    with contextlib.suppress(Exception), store._pool.connection() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM session_redirects")
        cur.execute("DELETE FROM session_registry")
    with contextlib.suppress(Exception):
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


class TestMiddlewareSubsequent:
    """Tests for subsequent request handling."""

    @pytest.mark.anyio
    async def test_known_session_passes_through(self, session_store: SessionStore) -> None:
        """Request with a known session_id passes through to FastMCP."""
        session_store.register(
            session_id="sess-known",
            owner_id="test-owner",
            node="app-a",
            protocol_version="2025-03-26",
            capabilities={},
            client_info={},
        )
        inner = await _make_fastmcp_stub(session_id="sess-known")
        mw = SessionRegistryMiddleware(inner, session_store, node_name="app-a")
        scope = _mcp_post_scope(session_id="sess-known")
        from mcp_awareness.server import _owner_ctx

        token = _owner_ctx.set("test-owner")
        try:
            status, _, _ = await _collect_response(mw, scope)
        finally:
            _owner_ctx.reset(token)
        assert status == 200

    @pytest.mark.anyio
    async def test_owner_mismatch_rejected(self, session_store: SessionStore) -> None:
        """Request where JWT owner differs from session owner returns 403."""
        session_store.register(
            session_id="sess-owned",
            owner_id="real-owner",
            node="app-a",
            protocol_version="2025-03-26",
            capabilities={},
            client_info={},
        )
        inner = await _make_fastmcp_stub()
        mw = SessionRegistryMiddleware(inner, session_store, node_name="app-a")
        scope = _mcp_post_scope(session_id="sess-owned")
        from mcp_awareness.server import _owner_ctx

        token = _owner_ctx.set("wrong-owner")
        try:
            status, _body, _ = await _collect_response(mw, scope)
        finally:
            _owner_ctx.reset(token)
        assert status == 403

    @pytest.mark.anyio
    async def test_unknown_session_passes_through(self, session_store: SessionStore) -> None:
        """Request with unknown session_id passes through to FastMCP."""
        inner = await _make_fastmcp_stub(status=400)
        mw = SessionRegistryMiddleware(inner, session_store, node_name="app-a")
        scope = _mcp_post_scope(session_id="sess-unknown")
        from mcp_awareness.server import _owner_ctx

        token = _owner_ctx.set("test-owner")
        try:
            status, _, _ = await _collect_response(mw, scope)
        finally:
            _owner_ctx.reset(token)
        assert status == 400

    @pytest.mark.anyio
    async def test_redirect_rewrites_session_id(self, session_store: SessionStore) -> None:
        """Request with old session_id that has a redirect is transparently rewritten."""
        session_store.register(
            session_id="new-sess",
            owner_id="test-owner",
            node="app-a",
            protocol_version="2025-03-26",
            capabilities={},
            client_info={},
        )
        session_store.add_redirect("old-sess", "new-sess")

        received_session_id = None

        async def checking_app(scope: Any, receive: Any, send: Any) -> None:
            nonlocal received_session_id
            hdrs = dict(scope.get("headers", []))
            received_session_id = hdrs.get(b"mcp-session-id", b"").decode()
            while True:
                msg = await receive()
                if msg.get("type") == "http.request" and not msg.get("more_body", False):
                    break
            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [(b"mcp-session-id", b"new-sess")],
                }
            )
            await send({"type": "http.response.body", "body": b'{"ok":true}'})

        mw = SessionRegistryMiddleware(checking_app, session_store, node_name="app-a")
        scope = _mcp_post_scope(session_id="old-sess")
        from mcp_awareness.server import _owner_ctx

        token = _owner_ctx.set("test-owner")
        try:
            status, _, _ = await _collect_response(mw, scope)
        finally:
            _owner_ctx.reset(token)
        assert status == 200
        assert received_session_id == "new-sess"

    @pytest.mark.anyio
    async def test_touch_debounced(self, session_store: SessionStore) -> None:
        """Touch only called when debounce interval has passed."""
        session_store.register(
            session_id="sess-debounce",
            owner_id="test-owner",
            node="app-a",
            protocol_version="2025-03-26",
            capabilities={},
            client_info={},
        )
        inner = await _make_fastmcp_stub(session_id="sess-debounce")
        mw = SessionRegistryMiddleware(inner, session_store, node_name="app-a")
        scope = _mcp_post_scope(session_id="sess-debounce")

        from mcp_awareness.server import _owner_ctx

        with patch.object(session_store, "touch") as mock_touch:
            token = _owner_ctx.set("test-owner")
            try:
                await _collect_response(mw, scope)
                assert mock_touch.call_count == 1
                await _collect_response(mw, scope)
                assert mock_touch.call_count == 1  # debounced
            finally:
                _owner_ctx.reset(token)


class TestMiddlewareReinit:
    """Tests for cross-node re-initialization."""

    @pytest.mark.anyio
    async def test_reinit_on_400_with_known_session(self, session_store: SessionStore) -> None:
        """When FastMCP returns 400 for a known session, middleware re-initializes."""
        session_store.register(
            session_id="old-sess",
            owner_id="test-owner",
            node="app-a",
            protocol_version="2025-03-26",
            capabilities={"roots": {"listChanged": True}},
            client_info={"name": "test-client", "version": "0.1"},
        )

        call_count = 0

        async def reinit_app(scope: Any, receive: Any, send: Any) -> None:
            nonlocal call_count
            call_count += 1
            while True:
                msg = await receive()
                if msg.get("type") == "http.request" and not msg.get("more_body", False):
                    break

            if call_count == 1:
                # Original request — session not in local memory
                await send(
                    {
                        "type": "http.response.start",
                        "status": 400,
                        "headers": [(b"content-type", b"text/plain")],
                    }
                )
                await send(
                    {
                        "type": "http.response.body",
                        "body": b"Bad Request: No valid session ID provided",
                    }
                )
            elif call_count == 2:
                # Synthetic initialize
                await send(
                    {
                        "type": "http.response.start",
                        "status": 200,
                        "headers": [
                            (b"content-type", b"application/json"),
                            (b"mcp-session-id", b"reinit-new-sess"),
                        ],
                    }
                )
                await send(
                    {
                        "type": "http.response.body",
                        "body": b'{"jsonrpc":"2.0","id":1,"result":{}}',
                    }
                )
            else:
                # Replayed original request
                await send(
                    {
                        "type": "http.response.start",
                        "status": 200,
                        "headers": [
                            (b"content-type", b"application/json"),
                            (b"mcp-session-id", b"reinit-new-sess"),
                        ],
                    }
                )
                await send(
                    {
                        "type": "http.response.body",
                        "body": b'{"jsonrpc":"2.0","id":2,"result":{"content":[]}}',
                    }
                )

        mw = SessionRegistryMiddleware(reinit_app, session_store, node_name="app-b")
        scope = _mcp_post_scope(session_id="old-sess")
        body = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "get_briefing"},
            }
        ).encode()

        from mcp_awareness.server import _owner_ctx

        token = _owner_ctx.set("test-owner")
        try:
            status, _resp_body, headers = await _collect_response(mw, scope, body)
        finally:
            _owner_ctx.reset(token)

        assert status == 200
        assert call_count == 3

        # New session registered
        new_session = session_store.lookup("reinit-new-sess")
        assert new_session is not None
        assert new_session["owner_id"] == "test-owner"
        assert new_session["node"] == "app-b"

        # Old session invalidated
        assert session_store.lookup("old-sess") is None

        # Redirect exists
        assert session_store.redirect_lookup("old-sess") == "reinit-new-sess"

        # Response carries new session_id
        session_header = dict(headers).get(b"mcp-session-id", b"").decode()
        assert session_header == "reinit-new-sess"

    @pytest.mark.anyio
    async def test_reinit_failure_returns_original_error(
        self,
        session_store: SessionStore,
    ) -> None:
        """If re-initialization itself fails, return the original 400."""
        session_store.register(
            session_id="failing-sess",
            owner_id="test-owner",
            node="app-a",
            protocol_version="2025-03-26",
            capabilities={},
            client_info={},
        )

        async def always_400_app(scope: Any, receive: Any, send: Any) -> None:
            while True:
                msg = await receive()
                if msg.get("type") == "http.request" and not msg.get("more_body", False):
                    break
            await send(
                {
                    "type": "http.response.start",
                    "status": 400,
                    "headers": [(b"content-type", b"text/plain")],
                }
            )
            await send(
                {
                    "type": "http.response.body",
                    "body": b"Bad Request: No valid session ID provided",
                }
            )

        mw = SessionRegistryMiddleware(always_400_app, session_store, node_name="app-a")
        scope = _mcp_post_scope(session_id="failing-sess")

        from mcp_awareness.server import _owner_ctx

        token = _owner_ctx.set("test-owner")
        try:
            status, _, _ = await _collect_response(mw, scope)
        finally:
            _owner_ctx.reset(token)

        assert status == 400

    @pytest.mark.anyio
    async def test_reinit_persist_failure_returns_original_400(
        self,
        session_store: SessionStore,
    ) -> None:
        """If re-init succeeds but persist fails, return original 400 (not untracked replay)."""
        session_store.register(
            session_id="persist-fail-sess",
            owner_id="test-owner",
            node="app-a",
            protocol_version="2025-03-26",
            capabilities={},
            client_info={},
        )

        call_count = 0

        async def reinit_app(scope: Any, receive: Any, send: Any) -> None:
            nonlocal call_count
            call_count += 1
            while True:
                msg = await receive()
                if msg.get("type") == "http.request" and not msg.get("more_body", False):
                    break
            if call_count == 1:
                # Original request — 400
                await send(
                    {
                        "type": "http.response.start",
                        "status": 400,
                        "headers": [(b"content-type", b"text/plain")],
                    }
                )
                await send(
                    {
                        "type": "http.response.body",
                        "body": b"Bad Request: No valid session ID provided",
                    }
                )
            else:
                # Synthetic initialize succeeds
                await send(
                    {
                        "type": "http.response.start",
                        "status": 200,
                        "headers": [
                            (b"content-type", b"application/json"),
                            (b"mcp-session-id", b"new-sess-persist-fail"),
                        ],
                    }
                )
                await send(
                    {"type": "http.response.body", "body": b'{"jsonrpc":"2.0","id":1,"result":{}}'}
                )

        mw = SessionRegistryMiddleware(reinit_app, session_store, node_name="app-a")
        scope = _mcp_post_scope(session_id="persist-fail-sess")

        from mcp_awareness.server import _owner_ctx

        # Make register fail after the synthetic init succeeds
        token = _owner_ctx.set("test-owner")
        try:
            with patch.object(session_store, "register", side_effect=Exception("DB down")):
                status, _, _ = await _collect_response(mw, scope)
        finally:
            _owner_ctx.reset(token)

        # Should get the original 400, NOT a replay on an untracked session
        assert status == 400
        assert call_count == 2  # original + synthetic init, no replay


class TestMiddlewareTerminate:
    """Tests for DELETE /mcp (session termination)."""

    @pytest.mark.anyio
    async def test_terminate_invalidates_session(self, session_store: SessionStore) -> None:
        """DELETE /mcp invalidates the session in the registry."""
        session_store.register(
            session_id="sess-to-terminate",
            owner_id="test-owner",
            node="app-a",
            protocol_version="2025-03-26",
            capabilities={},
            client_info={},
        )
        inner = await _make_fastmcp_stub()
        mw = SessionRegistryMiddleware(inner, session_store, node_name="app-a")
        scope = {
            "type": "http",
            "method": "DELETE",
            "path": "/mcp",
            "headers": [(b"mcp-session-id", b"sess-to-terminate")],
        }
        from mcp_awareness.server import _owner_ctx

        token = _owner_ctx.set("test-owner")
        try:
            status, _, _ = await _collect_response(mw, scope)
        finally:
            _owner_ctx.reset(token)
        assert status == 200
        assert session_store.lookup("sess-to-terminate") is None

    @pytest.mark.anyio
    async def test_terminate_cleans_redirects(self, session_store: SessionStore) -> None:
        """DELETE /mcp removes redirect mappings pointing to the terminated session."""
        session_store.register(
            session_id="sess-target",
            owner_id="test-owner",
            node="app-a",
            protocol_version="2025-03-26",
            capabilities={},
            client_info={},
        )
        session_store.add_redirect("old-redirect", "sess-target")
        inner = await _make_fastmcp_stub()
        mw = SessionRegistryMiddleware(inner, session_store, node_name="app-a")
        scope = {
            "type": "http",
            "method": "DELETE",
            "path": "/mcp",
            "headers": [(b"mcp-session-id", b"sess-target")],
        }
        from mcp_awareness.server import _owner_ctx

        token = _owner_ctx.set("test-owner")
        try:
            await _collect_response(mw, scope)
        finally:
            _owner_ctx.reset(token)
        assert session_store.redirect_lookup("old-redirect") is None


class TestMiddlewareSessionLimits:
    """Tests for per-owner session limits."""

    @pytest.mark.anyio
    async def test_session_limit_rejects_with_429(self, session_store: SessionStore) -> None:
        """Exceeding session limit returns 429."""
        for i in range(2):
            session_store.register(
                session_id=f"sess-{i}",
                owner_id="test-owner",
                node="app-a",
                protocol_version="2025-03-26",
                capabilities={},
                client_info={},
            )
        inner = await _make_fastmcp_stub(session_id="sess-new")
        mw = SessionRegistryMiddleware(
            inner,
            session_store,
            node_name="app-a",
            max_sessions_per_owner=2,
        )
        scope = _mcp_post_scope()
        body = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {}},
            }
        ).encode()
        from mcp_awareness.server import _owner_ctx

        token = _owner_ctx.set("test-owner")
        try:
            status, resp_body, _ = await _collect_response(mw, scope, body)
        finally:
            _owner_ctx.reset(token)
        assert status == 429
        assert b"limit" in resp_body.lower()

    @pytest.mark.anyio
    async def test_session_limit_allows_under_limit(self, session_store: SessionStore) -> None:
        """New sessions are allowed when under the limit."""
        session_store.register(
            session_id="sess-existing",
            owner_id="test-owner",
            node="app-a",
            protocol_version="2025-03-26",
            capabilities={},
            client_info={},
        )
        inner = await _make_fastmcp_stub(session_id="sess-new")
        mw = SessionRegistryMiddleware(
            inner,
            session_store,
            node_name="app-a",
            max_sessions_per_owner=5,
        )
        scope = _mcp_post_scope()
        body = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {}},
            }
        ).encode()
        from mcp_awareness.server import _owner_ctx

        token = _owner_ctx.set("test-owner")
        try:
            status, _, _ = await _collect_response(mw, scope, body)
        finally:
            _owner_ctx.reset(token)
        assert status == 200


class TestMiddlewareGracefulDegradation:
    """Tests for graceful degradation when session DB is unreachable."""

    @pytest.mark.anyio
    async def test_initialize_works_when_store_fails(
        self,
        session_store: SessionStore,
    ) -> None:
        """Initialize passes through even if registry write fails."""
        inner = await _make_fastmcp_stub(session_id="sess-degraded")
        mw = SessionRegistryMiddleware(inner, session_store, node_name="app-a")

        # Close the pool to simulate DB unreachable
        session_store._pool.close()

        scope = _mcp_post_scope()
        body = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {},
                },
            }
        ).encode()

        from mcp_awareness.server import _owner_ctx

        token = _owner_ctx.set("test-owner")
        try:
            status, _, _ = await _collect_response(mw, scope, body)
        finally:
            _owner_ctx.reset(token)

        # FastMCP processed it, registry write failed silently
        assert status == 200

    @pytest.mark.anyio
    async def test_subsequent_works_when_store_fails(
        self,
        session_store: SessionStore,
    ) -> None:
        """Subsequent requests pass through when registry lookup fails."""
        inner = await _make_fastmcp_stub(session_id="sess-ok")
        mw = SessionRegistryMiddleware(inner, session_store, node_name="app-a")

        # Close the pool to simulate DB unreachable
        session_store._pool.close()

        scope = _mcp_post_scope(session_id="sess-any")
        from mcp_awareness.server import _owner_ctx

        token = _owner_ctx.set("test-owner")
        try:
            status, _, _ = await _collect_response(mw, scope)
        finally:
            _owner_ctx.reset(token)

        # Passes through to FastMCP
        assert status == 200


class TestCoverageEdgeCases:
    """Edge-case tests targeting previously uncovered lines."""

    def test_schedule_cleanup_skips_when_thread_alive(self, session_store: SessionStore) -> None:
        """_schedule_cleanup is a no-op when cleanup thread is still running."""
        import threading

        # Force last_cleanup to 0 so debounce doesn't block
        session_store._last_cleanup = 0.0
        # Create a fake alive thread
        event = threading.Event()
        session_store._cleanup_thread = threading.Thread(target=event.wait, daemon=True)
        session_store._cleanup_thread.start()
        try:
            old_time = session_store._last_cleanup
            session_store._schedule_cleanup()
            # Should not have updated _last_cleanup (thread still alive)
            assert session_store._last_cleanup == old_time
        finally:
            event.set()
            session_store._cleanup_thread.join(timeout=1)

    def test_do_cleanup_logs_exception(self, session_store: SessionStore) -> None:
        """_do_cleanup catches and logs exceptions."""

        with patch.object(session_store, "cleanup_expired", side_effect=Exception("boom")):
            session_store._do_cleanup()  # Should not raise

    @pytest.mark.anyio
    async def test_initialize_with_malformed_body(self, session_store: SessionStore) -> None:
        """Initialize works even with non-JSON body (metadata extraction fails gracefully)."""
        inner = await _make_fastmcp_stub(session_id="sess-badjson")
        mw = SessionRegistryMiddleware(inner, session_store, node_name="app-a")
        scope = _mcp_post_scope()
        from mcp_awareness.server import _owner_ctx

        token = _owner_ctx.set("test-owner")
        try:
            status, _, _ = await _collect_response(mw, scope, b"not json at all")
        finally:
            _owner_ctx.reset(token)
        assert status == 200
        # Session registered but with empty metadata
        session = session_store.lookup("sess-badjson")
        assert session is not None
        assert session["capabilities"] == {}
        assert session["client_info"] == {}

    @pytest.mark.anyio
    async def test_subsequent_redirect_lookup_failure(self, session_store: SessionStore) -> None:
        """When redirect lookup fails, session is treated as not found (pass through)."""

        inner = await _make_fastmcp_stub(status=400)
        mw = SessionRegistryMiddleware(inner, session_store, node_name="app-a")
        scope = _mcp_post_scope(session_id="sess-redir-fail")
        from mcp_awareness.server import _owner_ctx

        # Session not in registry, redirect lookup will throw
        with patch.object(session_store, "redirect_lookup", side_effect=Exception("DB down")):
            token = _owner_ctx.set("test-owner")
            try:
                status, _, _ = await _collect_response(mw, scope)
            finally:
                _owner_ctx.reset(token)
        # Should pass through to FastMCP
        assert status == 400

    @pytest.mark.anyio
    async def test_subsequent_redirect_target_lookup_failure(
        self, session_store: SessionStore
    ) -> None:
        """When redirect target lookup fails, pass through to FastMCP."""

        session_store.add_redirect("old-redir-sess", "target-sess")
        inner = await _make_fastmcp_stub(status=400)
        mw = SessionRegistryMiddleware(inner, session_store, node_name="app-a")
        scope = _mcp_post_scope(session_id="old-redir-sess")
        from mcp_awareness.server import _owner_ctx

        # Patch lookup: return None first (triggering redirect path), then throw on redirect target
        call_count = 0

        def failing_lookup(sid: str) -> dict | None:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return None  # Initial lookup — not found
            raise Exception("DB down on redirect target")

        with patch.object(session_store, "lookup", side_effect=failing_lookup):
            token = _owner_ctx.set("test-owner")
            try:
                status, _, _ = await _collect_response(mw, scope)
            finally:
                _owner_ctx.reset(token)
        assert status == 400

    @pytest.mark.anyio
    async def test_reinit_no_session_id_in_init_response(self, session_store: SessionStore) -> None:
        """Re-init fails if synthetic initialize response has no session_id header."""
        session_store.register(
            session_id="sess-no-header",
            owner_id="test-owner",
            node="app-a",
            protocol_version="2025-03-26",
            capabilities={},
            client_info={},
        )
        call_count = 0

        async def no_header_app(scope: Any, receive: Any, send: Any) -> None:
            nonlocal call_count
            call_count += 1
            while True:
                msg = await receive()
                if msg.get("type") == "http.request" and not msg.get("more_body", False):
                    break
            if call_count == 1:
                await send(
                    {
                        "type": "http.response.start",
                        "status": 400,
                        "headers": [(b"content-type", b"text/plain")],
                    }
                )
                await send({"type": "http.response.body", "body": b"Bad Request"})
            else:
                # Init succeeds but NO mcp-session-id header
                await send(
                    {
                        "type": "http.response.start",
                        "status": 200,
                        "headers": [(b"content-type", b"application/json")],
                    }
                )
                await send(
                    {
                        "type": "http.response.body",
                        "body": b'{"jsonrpc":"2.0","id":1,"result":{}}',
                    }
                )

        mw = SessionRegistryMiddleware(no_header_app, session_store, node_name="app-a")
        scope = _mcp_post_scope(session_id="sess-no-header")
        from mcp_awareness.server import _owner_ctx

        token = _owner_ctx.set("test-owner")
        try:
            status, _, _ = await _collect_response(mw, scope)
        finally:
            _owner_ctx.reset(token)
        # Re-init failed — original 400 returned
        assert status == 400

    @pytest.mark.anyio
    async def test_terminate_exception_during_cleanup(self, session_store: SessionStore) -> None:
        """Terminate passes through even if registry cleanup fails."""

        session_store.register(
            session_id="sess-term-fail",
            owner_id="test-owner",
            node="app-a",
            protocol_version="2025-03-26",
            capabilities={},
            client_info={},
        )
        inner = await _make_fastmcp_stub()
        mw = SessionRegistryMiddleware(inner, session_store, node_name="app-a")
        scope = {
            "type": "http",
            "method": "DELETE",
            "path": "/mcp",
            "headers": [(b"mcp-session-id", b"sess-term-fail")],
        }
        from mcp_awareness.server import _owner_ctx

        with patch.object(session_store, "invalidate", side_effect=Exception("DB down")):
            token = _owner_ctx.set("test-owner")
            try:
                status, _, _ = await _collect_response(mw, scope)
            finally:
                _owner_ctx.reset(token)
        assert status == 200  # Pass through succeeded

    @pytest.mark.anyio
    async def test_touch_exception_swallowed(self, session_store: SessionStore) -> None:
        """Touch failure is logged but doesn't affect the response."""

        session_store.register(
            session_id="sess-touch-fail",
            owner_id="test-owner",
            node="app-a",
            protocol_version="2025-03-26",
            capabilities={},
            client_info={},
        )
        inner = await _make_fastmcp_stub(session_id="sess-touch-fail")
        mw = SessionRegistryMiddleware(inner, session_store, node_name="app-a")
        scope = _mcp_post_scope(session_id="sess-touch-fail")
        from mcp_awareness.server import _owner_ctx

        with patch.object(session_store, "touch", side_effect=Exception("DB down")):
            token = _owner_ctx.set("test-owner")
            try:
                status, _, _ = await _collect_response(mw, scope)
            finally:
                _owner_ctx.reset(token)
        assert status == 200

    @pytest.mark.anyio
    async def test_buffer_body_handles_disconnect(self, session_store: SessionStore) -> None:
        """_buffer_body handles http.disconnect gracefully."""

        async def disconnect_receive() -> dict:
            return {"type": "http.disconnect"}

        body, replay = await SessionRegistryMiddleware._buffer_body(disconnect_receive)
        assert body == b""
        # Replay after exhaustion returns disconnect
        msg = await replay()
        assert msg["type"] == "http.disconnect"
        # Second call to replay — StopIteration path
        msg2 = await replay()
        assert msg2["type"] == "http.disconnect"

    def test_extract_session_id_missing(self, session_store: SessionStore) -> None:
        """_extract_session_id returns None when header is absent."""
        result = SessionRegistryMiddleware._extract_session_id(
            [(b"content-type", b"application/json")]
        )
        assert result is None


class TestWrapWithSessionRegistry:
    """Tests for _wrap_with_session_registry in server.py."""

    def test_noop_when_no_url(self) -> None:
        """Returns app unchanged when SESSION_DATABASE_URL is empty."""
        from mcp_awareness import server as srv

        sentinel = object()
        with patch.object(srv, "SESSION_DATABASE_URL", ""):
            result = srv._wrap_with_session_registry(sentinel)
        assert result is sentinel

    def test_wraps_app_when_url_set(self, pg_dsn: str) -> None:
        """Returns SessionRegistryMiddleware when DATABASE_URL is set."""
        from mcp_awareness import server as srv

        sentinel = object()
        with (
            patch.object(srv, "SESSION_DATABASE_URL", pg_dsn),
            patch.object(srv, "SESSION_TTL", 300),
            patch.object(srv, "SESSION_POOL_MIN", 1),
            patch.object(srv, "SESSION_POOL_MAX", 2),
            patch.object(srv, "MAX_SESSIONS_PER_OWNER", 5),
            patch.object(srv, "SESSION_NODE_NAME", "test-node"),
        ):
            result = srv._wrap_with_session_registry(sentinel)
        assert isinstance(result, SessionRegistryMiddleware)
        assert result.app is sentinel
        assert result.node_name == "test-node"
        assert result.max_sessions_per_owner == 5
        result.store.close()


class TestEnsureDatabase:
    """Tests for automatic session database creation."""

    def test_creates_database_if_missing(self, pg_container: Any) -> None:
        """_ensure_database creates the database when it doesn't exist."""
        # Build a DSN pointing to a database that doesn't exist yet
        base_url = pg_container.get_connection_url().replace(
            "postgresql+psycopg2://", "postgresql://"
        )
        # Replace the database name with a unique test name
        from psycopg import conninfo

        params = conninfo.conninfo_to_dict(base_url)
        test_db = "test_session_auto_create"
        params["dbname"] = test_db
        test_dsn = conninfo.make_conninfo(**params)

        # Ensure it doesn't exist first
        admin_params = {**params, "dbname": "postgres"}
        admin_dsn = conninfo.make_conninfo(**admin_params)
        import psycopg

        with psycopg.connect(admin_dsn, autocommit=True) as conn, conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (test_db,))
            if cur.fetchone():
                cur.execute(
                    psycopg.sql.SQL("DROP DATABASE {}").format(psycopg.sql.Identifier(test_db))
                )

        try:
            # _ensure_database should create it
            SessionStore._ensure_database(test_dsn)

            # Verify it exists and has UTF-8 encoding
            with psycopg.connect(admin_dsn, autocommit=True) as conn, conn.cursor() as cur:
                cur.execute(
                    "SELECT encoding, datcollate FROM pg_database WHERE datname = %s",
                    (test_db,),
                )
                row = cur.fetchone()
                assert row is not None, "Database was not created"
                encoding, collate = row
                assert encoding == 6, f"Expected UTF8 (6), got {encoding}"  # 6 = UTF8
                assert collate == "C.UTF-8", f"Expected C.UTF-8, got {collate}"
        finally:
            # Clean up
            with psycopg.connect(admin_dsn, autocommit=True) as conn, conn.cursor() as cur:
                cur.execute(
                    psycopg.sql.SQL("DROP DATABASE IF EXISTS {}").format(
                        psycopg.sql.Identifier(test_db)
                    )
                )

    def test_idempotent_when_exists(self, pg_dsn: str) -> None:
        """_ensure_database is a no-op when the database already exists."""
        SessionStore._ensure_database(pg_dsn)  # Should not raise

    def test_skips_when_dbname_is_postgres(self) -> None:
        """_ensure_database is a no-op when target database is 'postgres'."""
        SessionStore._ensure_database("postgresql://user:pass@localhost/postgres")

    def test_skips_when_dbname_empty(self) -> None:
        """_ensure_database is a no-op when DSN has no database name."""
        SessionStore._ensure_database("postgresql://user:pass@localhost/")

    def test_handles_connection_failure(self) -> None:
        """_ensure_database degrades gracefully when it can't connect."""
        with patch("psycopg.connect", side_effect=Exception("connection refused")):
            # Should log debug and not raise
            SessionStore._ensure_database("postgresql://user:pass@localhost/nonexistent_db")


# ---------------------------------------------------------------------------
# Integration tests — real FastMCP + SessionRegistryMiddleware
# ---------------------------------------------------------------------------


class TestIntegrationWithFastMCP:
    """Integration tests using a real FastMCP ASGI app with session middleware.

    These tests verify that the middleware works correctly with the real MCP SDK
    session manager, not just ASGI stubs.
    """

    @pytest.mark.anyio
    async def test_initialize_and_tool_call(self, session_store: SessionStore) -> None:
        """Full flow: initialize → tools/list through real FastMCP + middleware."""
        import httpx
        from mcp.server.fastmcp import FastMCP
        from mcp.server.streamable_http import TransportSecuritySettings

        mcp = FastMCP(
            "test-session",
            json_response=True,
            transport_security=TransportSecuritySettings(
                enable_dns_rebinding_protection=False,
            ),
        )

        @mcp.tool()
        def ping() -> str:
            """A simple test tool."""
            return "pong"

        app = mcp.streamable_http_app()
        mw = SessionRegistryMiddleware(app, session_store, node_name="test-node")

        from mcp_awareness.server import _owner_ctx

        token = _owner_ctx.set("test-owner")
        try:
            async with mcp.session_manager.run():
                transport = httpx.ASGITransport(app=mw)
                async with httpx.AsyncClient(
                    transport=transport, base_url="http://testserver"
                ) as client:
                    # Step 1: Initialize
                    init_resp = await client.post(
                        "/mcp",
                        json={
                            "jsonrpc": "2.0",
                            "id": 1,
                            "method": "initialize",
                            "params": {
                                "protocolVersion": "2025-03-26",
                                "capabilities": {},
                                "clientInfo": {"name": "test", "version": "0.1"},
                            },
                        },
                        headers={"accept": "application/json, text/event-stream"},
                    )
                    assert init_resp.status_code == 200
                    session_id = init_resp.headers.get("mcp-session-id")
                    assert session_id is not None

                    # Verify session registered in store
                    session = session_store.lookup(session_id)
                    assert session is not None
                    assert session["owner_id"] == "test-owner"
                    assert session["node"] == "test-node"

                    # Step 2: Send initialized notification
                    notif_resp = await client.post(
                        "/mcp",
                        json={
                            "jsonrpc": "2.0",
                            "method": "notifications/initialized",
                        },
                        headers={
                            "mcp-session-id": session_id,
                            "accept": "application/json, text/event-stream",
                        },
                    )
                    assert notif_resp.status_code == 202

                    # Step 3: tools/list
                    list_resp = await client.post(
                        "/mcp",
                        json={
                            "jsonrpc": "2.0",
                            "id": 2,
                            "method": "tools/list",
                            "params": {},
                        },
                        headers={
                            "mcp-session-id": session_id,
                            "accept": "application/json, text/event-stream",
                        },
                    )
                    assert list_resp.status_code == 200
                    body = list_resp.json()
                    tool_names = [t["name"] for t in body["result"]["tools"]]
                    assert "ping" in tool_names

                    # Step 4: Terminate
                    del_resp = await client.request(
                        "DELETE",
                        "/mcp",
                        headers={"mcp-session-id": session_id},
                    )
                    assert del_resp.status_code == 200

                    # Verify session invalidated
                    assert session_store.lookup(session_id) is None
        finally:
            _owner_ctx.reset(token)

    @pytest.mark.anyio
    async def test_cross_node_reinit(self, session_store: SessionStore) -> None:
        """Simulate cross-node recovery: register session, start fresh FastMCP, verify re-init."""
        import httpx
        from mcp.server.fastmcp import FastMCP
        from mcp.server.streamable_http import TransportSecuritySettings

        no_security = TransportSecuritySettings(enable_dns_rebinding_protection=False)

        mcp1 = FastMCP("test-node-a", json_response=True, transport_security=no_security)

        @mcp1.tool()
        def echo(msg: str) -> str:
            """Echo tool."""
            return msg

        app1 = mcp1.streamable_http_app()
        mw1 = SessionRegistryMiddleware(app1, session_store, node_name="node-a")

        from mcp_awareness.server import _owner_ctx

        token = _owner_ctx.set("test-owner")
        try:
            # Step 1: Initialize on node A
            async with mcp1.session_manager.run():
                transport1 = httpx.ASGITransport(app=mw1)
                async with httpx.AsyncClient(
                    transport=transport1, base_url="http://testserver"
                ) as client1:
                    init_resp = await client1.post(
                        "/mcp",
                        json={
                            "jsonrpc": "2.0",
                            "id": 1,
                            "method": "initialize",
                            "params": {
                                "protocolVersion": "2025-03-26",
                                "capabilities": {},
                                "clientInfo": {"name": "test", "version": "0.1"},
                            },
                        },
                        headers={"accept": "application/json, text/event-stream"},
                    )
                    assert init_resp.status_code == 200
                    old_session_id = init_resp.headers.get("mcp-session-id")
                    assert old_session_id

                    # Send initialized notification
                    await client1.post(
                        "/mcp",
                        json={
                            "jsonrpc": "2.0",
                            "method": "notifications/initialized",
                        },
                        headers={
                            "mcp-session-id": old_session_id,
                            "accept": "application/json, text/event-stream",
                        },
                    )

            # Step 2: Create a NEW FastMCP app (simulates node restart / different node)
            mcp2 = FastMCP("test-node-b", json_response=True, transport_security=no_security)

            @mcp2.tool()
            def echo2(msg: str) -> str:
                """Echo tool."""
                return msg

            app2 = mcp2.streamable_http_app()
            mw2 = SessionRegistryMiddleware(app2, session_store, node_name="node-b")

            async with mcp2.session_manager.run():
                transport2 = httpx.ASGITransport(app=mw2)
                async with httpx.AsyncClient(
                    transport=transport2, base_url="http://testserver"
                ) as client2:
                    # Step 3: Send request with OLD session ID to NEW node
                    list_resp = await client2.post(
                        "/mcp",
                        json={
                            "jsonrpc": "2.0",
                            "id": 2,
                            "method": "tools/list",
                            "params": {},
                        },
                        headers={
                            "mcp-session-id": old_session_id,
                            "accept": "application/json, text/event-stream",
                        },
                    )
                    assert list_resp.status_code == 200

                    # Verify: new session_id in response
                    new_session_id = list_resp.headers.get("mcp-session-id")
                    assert new_session_id is not None
                    assert new_session_id != old_session_id

                    # Verify: redirect exists
                    assert session_store.redirect_lookup(old_session_id) == new_session_id

                    # Verify: old session invalidated, new registered
                    assert session_store.lookup(old_session_id) is None
                    new_session = session_store.lookup(new_session_id)
                    assert new_session is not None
                    assert new_session["node"] == "node-b"

                    # Verify: tools/list returned real data
                    body = list_resp.json()
                    tool_names = [t["name"] for t in body["result"]["tools"]]
                    assert "echo2" in tool_names
        finally:
            _owner_ctx.reset(token)
