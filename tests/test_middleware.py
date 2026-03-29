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

"""Tests for ASGI middleware classes (SecretPathMiddleware, HealthMiddleware, AuthMiddleware)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import MagicMock, patch

import jwt
import pytest

from mcp_awareness import server as server_mod
from mcp_awareness.middleware import (
    _FAVICON_BYTES,
    AuthMiddleware,
    HealthMiddleware,
    SecretPathMiddleware,
)


def _health_builder() -> dict[str, Any]:
    """Stable health response for testing."""
    return {
        "status": "ok",
        "uptime_sec": 42.0,
        "timestamp": "2026-03-26T00:00:00+00:00",
        "transport": "streamable-http",
    }


async def _dummy_app(scope: dict[str, Any], receive: Any, send: Any) -> None:
    """A minimal ASGI app that returns 200 with the path in the body."""
    body = json.dumps({"path": scope.get("path", "")}).encode()
    await send(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-type", b"application/json")],
        }
    )
    await send({"type": "http.response.body", "body": body})


async def _collect_response(app: Any, scope: dict[str, Any]) -> tuple[int, bytes]:
    """Send a request through an ASGI app and collect the response."""
    status_code = 0
    body_parts: list[bytes] = []

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b""}

    async def send(message: dict[str, Any]) -> None:
        nonlocal status_code
        if message["type"] == "http.response.start":
            status_code = message["status"]
        elif message["type"] == "http.response.body":
            body_parts.append(message.get("body", b""))

    await app(scope, receive, send)
    return status_code, b"".join(body_parts)


# ---------------------------------------------------------------------------
# SecretPathMiddleware
# ---------------------------------------------------------------------------


class TestSecretPathMiddleware:
    """Tests for SecretPathMiddleware."""

    def _make_app(self, prefix: str = "/secret") -> SecretPathMiddleware:
        return SecretPathMiddleware(_dummy_app, prefix, _health_builder)

    @pytest.mark.anyio
    async def test_path_rewriting(self) -> None:
        """Request to /secret/mcp is forwarded as /mcp."""
        app = self._make_app()
        scope = {"type": "http", "path": "/secret/mcp", "method": "POST"}
        status, body = await _collect_response(app, scope)
        assert status == 200
        data = json.loads(body)
        assert data["path"] == "/mcp"

    @pytest.mark.anyio
    async def test_health_endpoint(self) -> None:
        """Request to /secret/health returns JSON health response."""
        app = self._make_app()
        scope = {"type": "http", "path": "/secret/health", "method": "GET"}
        status, body = await _collect_response(app, scope)
        assert status == 200
        data = json.loads(body)
        assert data["status"] == "ok"
        assert data["uptime_sec"] == 42.0
        assert data["transport"] == "streamable-http"
        assert "timestamp" in data

    @pytest.mark.anyio
    async def test_non_secret_path_returns_404(self) -> None:
        """Request to a path not starting with the prefix returns 404."""
        app = self._make_app()
        scope = {"type": "http", "path": "/other/path", "method": "GET"}
        status, _body = await _collect_response(app, scope)
        assert status == 404

    @pytest.mark.anyio
    async def test_favicon_served_without_secret_path(self) -> None:
        """/favicon.ico is served publicly without requiring the secret prefix."""
        app = self._make_app()
        scope = {"type": "http", "path": "/favicon.ico", "method": "GET"}
        status, body = await _collect_response(app, scope)
        assert status == 200
        assert body == _FAVICON_BYTES

    @pytest.mark.anyio
    async def test_non_http_scope_passes_through(self) -> None:
        """Non-HTTP scope (e.g. lifespan) passes through to wrapped app."""
        calls: list[dict[str, Any]] = []

        async def tracking_app(scope: dict[str, Any], receive: Any, send: Any) -> None:
            calls.append(scope)

        app = SecretPathMiddleware(tracking_app, "/secret", _health_builder)
        scope = {"type": "lifespan"}
        await app(scope, lambda: None, lambda msg: None)  # type: ignore[arg-type, return-value]
        assert len(calls) == 1
        assert calls[0]["type"] == "lifespan"

    @pytest.mark.anyio
    async def test_trailing_slash_handling(self) -> None:
        """Request to /secret (no trailing path) forwards as /."""
        app = self._make_app()
        scope = {"type": "http", "path": "/secret", "method": "GET"}
        status, body = await _collect_response(app, scope)
        assert status == 200
        data = json.loads(body)
        assert data["path"] == "/"

    @pytest.mark.anyio
    async def test_prefix_with_trailing_slash(self) -> None:
        """Prefix with trailing slash is normalized."""
        app = self._make_app(prefix="/secret/")
        scope = {"type": "http", "path": "/secret/mcp", "method": "POST"}
        status, body = await _collect_response(app, scope)
        assert status == 200
        data = json.loads(body)
        assert data["path"] == "/mcp"

    @pytest.mark.anyio
    async def test_websocket_scope_rewritten(self) -> None:
        """WebSocket scope with secret prefix is also rewritten."""
        app = self._make_app()
        scope = {"type": "websocket", "path": "/secret/ws", "method": "GET"}
        status, body = await _collect_response(app, scope)
        assert status == 200
        data = json.loads(body)
        assert data["path"] == "/ws"


# ---------------------------------------------------------------------------
# HealthMiddleware
# ---------------------------------------------------------------------------


class TestHealthMiddleware:
    """Tests for HealthMiddleware."""

    def _make_app(self) -> HealthMiddleware:
        return HealthMiddleware(_dummy_app, _health_builder)

    @pytest.mark.anyio
    async def test_health_endpoint(self) -> None:
        """/health returns JSON with status, uptime_sec, timestamp, transport."""
        app = self._make_app()
        scope = {"type": "http", "path": "/health", "method": "GET"}
        status, body = await _collect_response(app, scope)
        assert status == 200
        data = json.loads(body)
        assert data["status"] == "ok"
        assert data["uptime_sec"] == 42.0
        assert data["transport"] == "streamable-http"
        assert "timestamp" in data

    @pytest.mark.anyio
    async def test_other_paths_pass_through(self) -> None:
        """Non-health HTTP paths pass through to wrapped app."""
        app = self._make_app()
        scope = {"type": "http", "path": "/mcp", "method": "POST"}
        status, body = await _collect_response(app, scope)
        assert status == 200
        data = json.loads(body)
        assert data["path"] == "/mcp"

    @pytest.mark.anyio
    async def test_favicon_served(self) -> None:
        """/favicon.ico returns the favicon with correct content type."""
        app = self._make_app()
        scope = {"type": "http", "path": "/favicon.ico", "method": "GET"}
        status, body = await _collect_response(app, scope)
        assert status == 200
        assert body == _FAVICON_BYTES

    @pytest.mark.anyio
    async def test_non_http_scope_passes_through(self) -> None:
        """Non-HTTP scope passes through to wrapped app."""
        calls: list[dict[str, Any]] = []

        async def tracking_app(scope: dict[str, Any], receive: Any, send: Any) -> None:
            calls.append(scope)

        app = HealthMiddleware(tracking_app, _health_builder)
        scope = {"type": "lifespan"}
        await app(scope, lambda: None, lambda msg: None)  # type: ignore[arg-type, return-value]
        assert len(calls) == 1
        assert calls[0]["type"] == "lifespan"


# ---------------------------------------------------------------------------
# _run() transport wiring tests
# ---------------------------------------------------------------------------


class TestRunTransportWiring:
    """Tests for _run() to verify middleware is wired correctly per transport."""

    def test_http_with_mount_path_uses_secret_path_middleware(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """streamable-http + MOUNT_PATH wires SecretPathMiddleware."""
        monkeypatch.setattr(server_mod, "TRANSPORT", "streamable-http")
        monkeypatch.setattr(server_mod, "MOUNT_PATH", "/secret")
        monkeypatch.setattr(server_mod, "HOST", "0.0.0.0")
        monkeypatch.setattr(server_mod, "PORT", 8080)

        mock_app = MagicMock()
        monkeypatch.setattr(server_mod.mcp, "streamable_http_app", lambda: mock_app)

        captured_app: list[Any] = []

        def fake_config(app: Any, **kwargs: Any) -> MagicMock:
            captured_app.append(app)
            return MagicMock()

        with patch("uvicorn.Config", side_effect=fake_config), patch("anyio.run"):
            server_mod._run()

        assert len(captured_app) == 1
        assert isinstance(captured_app[0], SecretPathMiddleware)

    def test_http_without_mount_path_uses_health_middleware(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """streamable-http without MOUNT_PATH wires HealthMiddleware."""
        monkeypatch.setattr(server_mod, "TRANSPORT", "streamable-http")
        monkeypatch.setattr(server_mod, "MOUNT_PATH", "")
        monkeypatch.setattr(server_mod, "HOST", "0.0.0.0")
        monkeypatch.setattr(server_mod, "PORT", 8080)

        mock_app = MagicMock()
        monkeypatch.setattr(server_mod.mcp, "streamable_http_app", lambda: mock_app)

        captured_app: list[Any] = []

        def fake_config(app: Any, **kwargs: Any) -> MagicMock:
            captured_app.append(app)
            return MagicMock()

        with patch("uvicorn.Config", side_effect=fake_config), patch("anyio.run"):
            server_mod._run()

        assert len(captured_app) == 1
        assert isinstance(captured_app[0], HealthMiddleware)

    def test_stdio_transport_calls_mcp_run(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Non-HTTP transport calls mcp.run(transport=...)."""
        monkeypatch.setattr(server_mod, "TRANSPORT", "stdio")
        called_with: list[str] = []
        monkeypatch.setattr(server_mod.mcp, "run", lambda transport: called_with.append(transport))
        server_mod._run()
        assert called_with == ["stdio"]

    def test_http_with_mount_path_auth_required_wraps_with_auth_middleware(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """streamable-http + MOUNT_PATH + AUTH_REQUIRED wires AuthMiddleware outermost."""
        monkeypatch.setattr(server_mod, "TRANSPORT", "streamable-http")
        monkeypatch.setattr(server_mod, "MOUNT_PATH", "/secret")
        monkeypatch.setattr(server_mod, "HOST", "0.0.0.0")
        monkeypatch.setattr(server_mod, "PORT", 8080)
        monkeypatch.setattr(server_mod, "AUTH_REQUIRED", True)
        monkeypatch.setattr(server_mod, "JWT_SECRET", "test-secret-key")
        monkeypatch.setattr(server_mod, "JWT_ALGORITHM", "HS256")

        mock_app = MagicMock()
        monkeypatch.setattr(server_mod.mcp, "streamable_http_app", lambda: mock_app)

        captured_app: list[Any] = []

        def fake_config(app: Any, **kwargs: Any) -> MagicMock:
            captured_app.append(app)
            return MagicMock()

        with patch("uvicorn.Config", side_effect=fake_config), patch("anyio.run"):
            server_mod._run()

        assert len(captured_app) == 1
        assert isinstance(captured_app[0], AuthMiddleware)

    def test_http_auth_required_no_secret_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """AUTH_REQUIRED without JWT_SECRET raises ValueError."""
        monkeypatch.setattr(server_mod, "TRANSPORT", "streamable-http")
        monkeypatch.setattr(server_mod, "MOUNT_PATH", "")
        monkeypatch.setattr(server_mod, "HOST", "0.0.0.0")
        monkeypatch.setattr(server_mod, "PORT", 8080)
        monkeypatch.setattr(server_mod, "AUTH_REQUIRED", True)
        monkeypatch.setattr(server_mod, "JWT_SECRET", "")

        mock_app = MagicMock()
        monkeypatch.setattr(server_mod.mcp, "streamable_http_app", lambda: mock_app)

        with pytest.raises(ValueError, match="AWARENESS_JWT_SECRET is required"):
            server_mod._run()


# ---------------------------------------------------------------------------
# AuthMiddleware
# ---------------------------------------------------------------------------

_JWT_SECRET = "test-secret-for-middleware-tests"


class TestAuthMiddleware:
    """Tests for AuthMiddleware."""

    def _make_app(self) -> AuthMiddleware:
        return AuthMiddleware(_dummy_app, _JWT_SECRET)

    def _make_token(
        self,
        sub: str = "alice",
        exp_delta: timedelta | None = None,
        secret: str = _JWT_SECRET,
    ) -> str:
        now = datetime.now(timezone.utc)
        payload: dict[str, Any] = {"sub": sub, "iat": now}
        if exp_delta is not None:
            payload["exp"] = now + exp_delta
        else:
            payload["exp"] = now + timedelta(hours=1)
        return jwt.encode(payload, secret, algorithm="HS256")

    @pytest.mark.anyio
    async def test_valid_jwt_passes_request(self) -> None:
        """Valid JWT allows the request through and sets owner context."""
        app = self._make_app()
        token = self._make_token(sub="alice")
        scope = {
            "type": "http",
            "path": "/mcp",
            "method": "POST",
            "headers": [(b"authorization", f"Bearer {token}".encode())],
        }
        status, _body = await _collect_response(app, scope)
        assert status == 200

    @pytest.mark.anyio
    async def test_expired_jwt_returns_401(self) -> None:
        """Expired JWT returns 401."""
        app = self._make_app()
        token = self._make_token(exp_delta=timedelta(hours=-1))
        scope = {
            "type": "http",
            "path": "/mcp",
            "method": "POST",
            "headers": [(b"authorization", f"Bearer {token}".encode())],
        }
        status, body = await _collect_response(app, scope)
        assert status == 401
        data = json.loads(body)
        assert "expired" in data["error"].lower()

    @pytest.mark.anyio
    async def test_missing_auth_header_returns_401(self) -> None:
        """Missing Authorization header returns 401."""
        app = self._make_app()
        scope = {
            "type": "http",
            "path": "/mcp",
            "method": "POST",
            "headers": [],
        }
        status, body = await _collect_response(app, scope)
        assert status == 401
        data = json.loads(body)
        assert "missing" in data["error"].lower() or "invalid" in data["error"].lower()

    @pytest.mark.anyio
    async def test_invalid_token_returns_401(self) -> None:
        """Invalid token (wrong secret) returns 401."""
        app = self._make_app()
        token = self._make_token(secret="wrong-secret")
        scope = {
            "type": "http",
            "path": "/mcp",
            "method": "POST",
            "headers": [(b"authorization", f"Bearer {token}".encode())],
        }
        status, body = await _collect_response(app, scope)
        assert status == 401
        data = json.loads(body)
        assert "invalid" in data["error"].lower()

    @pytest.mark.anyio
    async def test_jwt_missing_sub_returns_401(self) -> None:
        """JWT without 'sub' claim returns 401."""
        app = self._make_app()
        now = datetime.now(timezone.utc)
        payload = {"iat": now, "exp": now + timedelta(hours=1)}
        token = jwt.encode(payload, _JWT_SECRET, algorithm="HS256")
        scope = {
            "type": "http",
            "path": "/mcp",
            "method": "POST",
            "headers": [(b"authorization", f"Bearer {token}".encode())],
        }
        status, body = await _collect_response(app, scope)
        assert status == 401
        data = json.loads(body)
        assert "sub" in data["error"].lower()

    @pytest.mark.anyio
    async def test_health_path_skips_auth(self) -> None:
        """/health bypasses JWT auth."""
        app = self._make_app()
        scope = {
            "type": "http",
            "path": "/health",
            "method": "GET",
            "headers": [],
        }
        status, _body = await _collect_response(app, scope)
        assert status == 200

    @pytest.mark.anyio
    async def test_favicon_path_skips_auth(self) -> None:
        """/favicon.ico bypasses JWT auth."""
        app = self._make_app()
        scope = {
            "type": "http",
            "path": "/favicon.ico",
            "method": "GET",
            "headers": [],
        }
        status, _body = await _collect_response(app, scope)
        assert status == 200

    @pytest.mark.anyio
    async def test_non_http_scope_passes_through(self) -> None:
        """Non-HTTP scope (e.g. lifespan) passes through without auth."""
        calls: list[dict[str, Any]] = []

        async def tracking_app(scope: dict[str, Any], receive: Any, send: Any) -> None:
            calls.append(scope)

        app = AuthMiddleware(tracking_app, _JWT_SECRET)
        scope = {"type": "lifespan"}
        await app(scope, lambda: None, lambda msg: None)  # type: ignore[arg-type, return-value]
        assert len(calls) == 1
        assert calls[0]["type"] == "lifespan"

    @pytest.mark.anyio
    async def test_owner_context_set_from_jwt(self) -> None:
        """Valid JWT sets the _owner_ctx contextvar to the sub claim."""
        captured_owner: list[str] = []

        async def capture_app(scope: dict[str, Any], receive: Any, send: Any) -> None:
            from mcp_awareness.server import _owner_ctx

            captured_owner.append(_owner_ctx.get())
            await _dummy_app(scope, receive, send)

        app = AuthMiddleware(capture_app, _JWT_SECRET)
        token = self._make_token(sub="bob")
        scope = {
            "type": "http",
            "path": "/mcp",
            "method": "POST",
            "headers": [(b"authorization", f"Bearer {token}".encode())],
        }
        status, _ = await _collect_response(app, scope)
        assert status == 200
        assert captured_owner == ["bob"]

    @pytest.mark.anyio
    async def test_unexpected_exception_re_raises(self) -> None:
        """Non-JWT exceptions during decode are re-raised, not swallowed."""

        async def inner_app(scope: Any, receive: Any, send: Any) -> None:
            pass  # never reached

        app = AuthMiddleware(inner_app, _JWT_SECRET)
        scope = {
            "type": "http",
            "path": "/mcp",
            "method": "POST",
            "headers": [(b"authorization", b"Bearer valid-looking-token")],
        }

        async def noop_receive() -> dict[str, Any]:
            return {"type": "http.request", "body": b""}

        async def noop_send(msg: dict[str, Any]) -> None:
            pass

        with (
            patch("jwt.decode", side_effect=RuntimeError("boom")),
            pytest.raises(RuntimeError, match="boom"),
        ):
            await app(scope, noop_receive, noop_send)


class TestServerAuthWiring:
    """Additional server wiring tests for auth coverage."""

    def test_http_with_mount_path_auth_no_secret_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AUTH_REQUIRED + MOUNT_PATH without JWT_SECRET raises ValueError."""
        monkeypatch.setattr(server_mod, "TRANSPORT", "streamable-http")
        monkeypatch.setattr(server_mod, "MOUNT_PATH", "/secret")
        monkeypatch.setattr(server_mod, "HOST", "0.0.0.0")
        monkeypatch.setattr(server_mod, "PORT", 8080)
        monkeypatch.setattr(server_mod, "AUTH_REQUIRED", True)
        monkeypatch.setattr(server_mod, "JWT_SECRET", "")

        mock_app = MagicMock()
        monkeypatch.setattr(server_mod.mcp, "streamable_http_app", lambda: mock_app)

        with pytest.raises(ValueError, match="AWARENESS_JWT_SECRET is required"):
            server_mod._run()

    def test_http_no_mount_path_auth_required_wraps_with_auth_middleware(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """streamable-http without MOUNT_PATH + AUTH_REQUIRED wires AuthMiddleware."""
        monkeypatch.setattr(server_mod, "TRANSPORT", "streamable-http")
        monkeypatch.setattr(server_mod, "MOUNT_PATH", "")
        monkeypatch.setattr(server_mod, "HOST", "0.0.0.0")
        monkeypatch.setattr(server_mod, "PORT", 8080)
        monkeypatch.setattr(server_mod, "AUTH_REQUIRED", True)
        monkeypatch.setattr(server_mod, "JWT_SECRET", "test-secret-key")
        monkeypatch.setattr(server_mod, "JWT_ALGORITHM", "HS256")

        mock_app = MagicMock()
        monkeypatch.setattr(server_mod.mcp, "streamable_http_app", lambda: mock_app)

        captured_app: list[Any] = []

        def fake_config(app: Any, **kwargs: Any) -> MagicMock:
            captured_app.append(app)
            return MagicMock()

        with patch("uvicorn.Config", side_effect=fake_config), patch("anyio.run"):
            server_mod._run()

        assert len(captured_app) == 1
        assert isinstance(captured_app[0], AuthMiddleware)
