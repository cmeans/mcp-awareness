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

"""Tests for OAuth proxy workaround middleware."""

from __future__ import annotations

import json
import time
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from mcp_awareness.oauth_proxy import (
    OAuthProxyMiddleware,
    ProxyStats,
    RateLimiter,
    detect_bogus_request,
    discover_oidc_endpoints,
    resolve_client_ip,
)


class TestResolveClientIp:
    """Tests for resolve_client_ip()."""

    def test_cf_connecting_ip_preferred(self) -> None:
        """CF-Connecting-IP is used when present (default header chain)."""
        headers = {
            b"cf-connecting-ip": b"1.2.3.4",
            b"x-real-ip": b"5.6.7.8",
        }
        scope = {"headers": list(headers.items()), "client": ("10.0.0.1", 12345)}
        assert resolve_client_ip(scope) == "1.2.3.4"

    def test_x_real_ip_fallback(self) -> None:
        """X-Real-IP is used when CF-Connecting-IP is absent."""
        headers = {b"x-real-ip": b"5.6.7.8"}
        scope = {"headers": list(headers.items()), "client": ("10.0.0.1", 12345)}
        assert resolve_client_ip(scope) == "5.6.7.8"

    def test_asgi_client_fallback(self) -> None:
        """Falls back to ASGI client address when no trusted headers found."""
        scope = {"headers": [], "client": ("10.0.0.1", 12345)}
        assert resolve_client_ip(scope) == "10.0.0.1"

    def test_no_client_at_all(self) -> None:
        """Returns 'unknown' when no headers and no ASGI client."""
        scope = {"headers": [], "client": None}
        assert resolve_client_ip(scope) == "unknown"

    def test_custom_header_chain(self) -> None:
        """Custom IP_HEADERS override the default chain."""
        headers = {b"x-amzn-source-ip": b"9.8.7.6"}
        scope = {"headers": list(headers.items()), "client": ("10.0.0.1", 12345)}
        result = resolve_client_ip(scope, ip_headers=["X-Amzn-Source-Ip"])
        assert result == "9.8.7.6"


class TestRateLimiter:
    """Tests for RateLimiter."""

    def test_allows_under_limit(self) -> None:
        """Requests under the limit are allowed."""
        rl = RateLimiter(max_requests=5, window_seconds=60)
        for _ in range(5):
            assert rl.check("1.2.3.4") is True

    def test_blocks_over_limit(self) -> None:
        """Sixth request in the window is blocked."""
        rl = RateLimiter(max_requests=5, window_seconds=60)
        for _ in range(5):
            rl.check("1.2.3.4")
        assert rl.check("1.2.3.4") is False

    def test_per_ip_isolation(self) -> None:
        """Rate limits are tracked per IP."""
        rl = RateLimiter(max_requests=1, window_seconds=60)
        assert rl.check("1.1.1.1") is True
        assert rl.check("1.1.1.1") is False
        assert rl.check("2.2.2.2") is True

    def test_window_expiry(self) -> None:
        """Old timestamps are pruned and the request is allowed."""
        rl = RateLimiter(max_requests=1, window_seconds=1)
        assert rl.check("1.1.1.1") is True
        assert rl.check("1.1.1.1") is False
        with patch("mcp_awareness.oauth_proxy.time.monotonic", return_value=time.monotonic() + 2):
            assert rl.check("1.1.1.1") is True

    def test_ban_blocks_all_requests(self) -> None:
        """A banned IP is rejected regardless of rate limit."""
        rl = RateLimiter(max_requests=100, window_seconds=60, ban_duration=3600)
        rl.ban("1.1.1.1", reason="test")
        assert rl.check("1.1.1.1") is False

    def test_ban_expires(self) -> None:
        """Bans expire after ban_duration seconds."""
        rl = RateLimiter(max_requests=100, window_seconds=60, ban_duration=10)
        rl.ban("1.1.1.1", reason="test")
        assert rl.check("1.1.1.1") is False
        with patch("mcp_awareness.oauth_proxy.time.monotonic", return_value=time.monotonic() + 11):
            assert rl.check("1.1.1.1") is True

    def test_stats(self) -> None:
        """Stats report rate-limited and banned counts."""
        rl = RateLimiter(max_requests=1, window_seconds=60, ban_duration=3600)
        rl.check("1.1.1.1")
        rl.check("1.1.1.1")  # rate limited
        rl.ban("2.2.2.2", reason="test")
        stats = rl.stats()
        assert stats["rate_limited"] >= 1
        assert stats["banned_ips"] == 1


class TestDetectBogusRequest:
    """Tests for detect_bogus_request()."""

    def test_valid_authorize_passes(self) -> None:
        """A valid /authorize request is not flagged."""
        result = detect_bogus_request(
            "/authorize",
            "GET",
            {"response_type": "code", "client_id": "abc", "redirect_uri": "https://example.com/cb"},
        )
        assert result is None

    def test_authorize_missing_client_id(self) -> None:
        """Missing client_id on /authorize is flagged."""
        result = detect_bogus_request(
            "/authorize",
            "GET",
            {"response_type": "code", "redirect_uri": "https://example.com/cb"},
        )
        assert result is not None
        assert "client_id" in result

    def test_authorize_missing_response_type(self) -> None:
        """Missing response_type on /authorize is flagged."""
        result = detect_bogus_request(
            "/authorize",
            "GET",
            {"client_id": "abc", "redirect_uri": "https://example.com/cb"},
        )
        assert result is not None

    def test_authorize_missing_redirect_uri(self) -> None:
        """Missing redirect_uri on /authorize is flagged."""
        result = detect_bogus_request(
            "/authorize",
            "GET",
            {"response_type": "code", "client_id": "abc"},
        )
        assert result is not None

    def test_wrong_method_post_authorize(self) -> None:
        """POST to /authorize is flagged (should be GET)."""
        result = detect_bogus_request(
            "/authorize",
            "POST",
            {"response_type": "code", "client_id": "abc", "redirect_uri": "https://example.com/cb"},
        )
        assert result is not None
        assert "method" in result.lower()

    def test_wrong_method_get_token(self) -> None:
        """GET to /token is flagged (should be POST)."""
        result = detect_bogus_request("/token", "GET", {})
        assert result is not None
        assert "method" in result.lower()

    def test_injection_in_params(self) -> None:
        """Path traversal in param values is flagged."""
        result = detect_bogus_request(
            "/authorize",
            "GET",
            {
                "response_type": "code",
                "client_id": "../../etc/passwd",
                "redirect_uri": "https://x.com/cb",
            },
        )
        assert result is not None
        assert "injection" in result.lower()

    def test_sql_injection_in_params(self) -> None:
        """SQL injection pattern in param values is flagged."""
        result = detect_bogus_request(
            "/authorize",
            "GET",
            {
                "response_type": "code",
                "client_id": "'; DROP TABLE users;--",
                "redirect_uri": "https://x.com/cb",
            },
        )
        assert result is not None

    def test_valid_token_post_passes(self) -> None:
        """POST to /token is not flagged."""
        result = detect_bogus_request("/token", "POST", {})
        assert result is None

    def test_valid_register_post_passes(self) -> None:
        """POST to /register is not flagged."""
        result = detect_bogus_request("/register", "POST", {})
        assert result is None


class TestDiscoverOidcEndpoints:
    """Tests for discover_oidc_endpoints()."""

    def _mock_oidc_response(self, config: dict[str, str]) -> MagicMock:
        """Create a mock urllib response with the given JSON body."""
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(config).encode()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        return mock_resp

    def test_discovers_all_endpoints(self) -> None:
        """All three endpoints are extracted from OIDC config."""
        config = {
            "authorization_endpoint": "https://auth.example.com/authorize",
            "token_endpoint": "https://auth.example.com/token",
            "registration_endpoint": "https://auth.example.com/register",
        }
        with patch("urllib.request.urlopen", return_value=self._mock_oidc_response(config)):
            result = discover_oidc_endpoints("https://auth.example.com")
        assert result["authorization_endpoint"] == "https://auth.example.com/authorize"
        assert result["token_endpoint"] == "https://auth.example.com/token"
        assert result["registration_endpoint"] == "https://auth.example.com/register"

    def test_missing_registration_endpoint(self) -> None:
        """Registration endpoint is None when not in OIDC config."""
        config = {
            "authorization_endpoint": "https://auth.example.com/authorize",
            "token_endpoint": "https://auth.example.com/token",
        }
        with patch("urllib.request.urlopen", return_value=self._mock_oidc_response(config)):
            result = discover_oidc_endpoints("https://auth.example.com")
        assert result["authorization_endpoint"] == "https://auth.example.com/authorize"
        assert result["token_endpoint"] == "https://auth.example.com/token"
        assert result["registration_endpoint"] is None

    def test_discovery_failure_returns_none(self) -> None:
        """Returns None when OIDC discovery fails."""
        with patch("urllib.request.urlopen", side_effect=Exception("connection refused")):
            result = discover_oidc_endpoints("https://auth.example.com")
        assert result is None

    def test_missing_required_endpoints_returns_none(self) -> None:
        """Returns None when authorization_endpoint or token_endpoint is missing."""
        config = {"authorization_endpoint": "https://auth.example.com/authorize"}
        with patch("urllib.request.urlopen", return_value=self._mock_oidc_response(config)):
            result = discover_oidc_endpoints("https://auth.example.com")
        assert result is None


class TestProxyStats:
    """Tests for ProxyStats health reporting."""

    def test_initial_stats(self) -> None:
        """Fresh stats are all zeros/None."""
        stats = ProxyStats()
        data = stats.to_dict()
        assert data["completed_flows"] == 0
        assert data["last_completed_flow"] is None
        assert data["raw_hits"] == {"authorize": 0, "token": 0, "register": 0}

    def test_record_hit(self) -> None:
        """Hits are counted per route."""
        stats = ProxyStats()
        stats.record_hit("authorize")
        stats.record_hit("token")
        stats.record_hit("token")
        data = stats.to_dict()
        assert data["raw_hits"]["authorize"] == 1
        assert data["raw_hits"]["token"] == 2
        assert data["raw_hits"]["register"] == 0

    def test_record_completed_flow(self) -> None:
        """Completed flows are counted and timestamped."""
        stats = ProxyStats()
        stats.record_completed_flow()
        data = stats.to_dict()
        assert data["completed_flows"] == 1
        assert data["last_completed_flow"] is not None


# ---------------------------------------------------------------------------
# OAuthProxyMiddleware helpers and tests
# ---------------------------------------------------------------------------


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


async def _collect_response(
    app: Any, scope: dict[str, Any], body: bytes = b""
) -> tuple[int, bytes, list[tuple[bytes, bytes]]]:
    """Send a request through an ASGI app and collect status, body, headers."""
    status = 0
    resp_body = b""
    headers: list[tuple[bytes, bytes]] = []

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": body}

    async def send(message: dict[str, Any]) -> None:
        nonlocal status, resp_body, headers
        if message["type"] == "http.response.start":
            status = message["status"]
            headers = message.get("headers", [])
        elif message["type"] == "http.response.body":
            resp_body += message.get("body", b"")

    await app(scope, receive, send)
    return status, resp_body, headers


def _make_middleware(
    endpoints: dict[str, str | None] | None = None,
) -> OAuthProxyMiddleware:
    """Create an OAuthProxyMiddleware with test endpoints."""
    ep = endpoints or {
        "authorization_endpoint": "https://auth.example.com/authorize",
        "token_endpoint": "https://auth.example.com/token",
        "registration_endpoint": "https://auth.example.com/register",
    }
    return OAuthProxyMiddleware(
        app=_dummy_app,
        endpoints=ep,
        ban_duration=3600,
        ip_headers=["CF-Connecting-IP", "X-Real-IP"],
    )


class TestOAuthProxyMiddleware:
    """Tests for OAuthProxyMiddleware ASGI handling."""

    @pytest.mark.anyio
    async def test_passthrough_non_oauth_path(self) -> None:
        """Non-OAuth paths pass through to the inner app."""
        mw = _make_middleware()
        scope = {
            "type": "http",
            "path": "/mcp",
            "method": "POST",
            "headers": [],
            "client": ("1.1.1.1", 1234),
        }
        status, body, _ = await _collect_response(mw, scope)
        assert status == 200
        data = json.loads(body)
        assert data["path"] == "/mcp"

    @pytest.mark.anyio
    async def test_authorize_redirects(self) -> None:
        """GET /authorize returns 302 to the real authorization endpoint."""
        mw = _make_middleware()
        scope = {
            "type": "http",
            "path": "/authorize",
            "method": "GET",
            "query_string": (
                b"response_type=code&client_id=abc"
                b"&redirect_uri=https%3A%2F%2Fexample.com%2Fcb&state=xyz"
            ),
            "headers": [],
            "client": ("1.1.1.1", 1234),
        }
        status, _, headers = await _collect_response(mw, scope)
        assert status == 302
        location = dict(headers).get(b"location", b"").decode()
        assert location.startswith("https://auth.example.com/authorize?")
        assert "client_id=abc" in location
        assert "state=xyz" in location

    @pytest.mark.anyio
    async def test_token_proxies_to_upstream(self) -> None:
        """POST /token proxies to the real token endpoint and relays the response."""
        mw = _make_middleware()
        scope = {
            "type": "http",
            "path": "/token",
            "method": "POST",
            "headers": [(b"content-type", b"application/x-www-form-urlencoded")],
            "client": ("1.1.1.1", 1234),
        }
        upstream_body = json.dumps({"access_token": "tok_123", "token_type": "bearer"}).encode()
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = upstream_body
        mock_resp.getheader.side_effect = lambda h, d=None: {
            "Content-Type": "application/json",
        }.get(h, d)
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_resp):
            status, body, _headers = await _collect_response(
                mw, scope, body=b"grant_type=authorization_code&code=abc"
            )
        assert status == 200
        data = json.loads(body)
        assert data["access_token"] == "tok_123"

    @pytest.mark.anyio
    async def test_cors_preflight_token(self) -> None:
        """OPTIONS /token returns CORS headers."""
        mw = _make_middleware()
        scope = {
            "type": "http",
            "path": "/token",
            "method": "OPTIONS",
            "headers": [],
            "client": ("1.1.1.1", 1234),
        }
        status, _, headers = await _collect_response(mw, scope)
        assert status == 204
        header_dict = dict(headers)
        assert b"access-control-allow-origin" in header_dict
        assert b"access-control-allow-methods" in header_dict

    @pytest.mark.anyio
    async def test_cors_preflight_register(self) -> None:
        """OPTIONS /register returns CORS headers."""
        mw = _make_middleware()
        scope = {
            "type": "http",
            "path": "/register",
            "method": "OPTIONS",
            "headers": [],
            "client": ("1.1.1.1", 1234),
        }
        status, _, _headers = await _collect_response(mw, scope)
        assert status == 204

    @pytest.mark.anyio
    async def test_register_404_when_not_discovered(self) -> None:
        """POST /register returns 404 when registration_endpoint was not discovered."""
        mw = _make_middleware(
            endpoints={
                "authorization_endpoint": "https://auth.example.com/authorize",
                "token_endpoint": "https://auth.example.com/token",
                "registration_endpoint": None,
            }
        )
        scope = {
            "type": "http",
            "path": "/register",
            "method": "POST",
            "headers": [(b"content-type", b"application/json")],
            "client": ("1.1.1.1", 1234),
        }
        status, _, _ = await _collect_response(mw, scope)
        assert status == 404

    @pytest.mark.anyio
    async def test_bogus_request_returns_403(self) -> None:
        """A request with injection patterns returns 403."""
        mw = _make_middleware()
        scope = {
            "type": "http",
            "path": "/authorize",
            "method": "GET",
            "query_string": b"response_type=code&client_id=../../etc/passwd&redirect_uri=https://x.com/cb",
            "headers": [],
            "client": ("1.1.1.1", 1234),
        }
        status, _, _ = await _collect_response(mw, scope)
        assert status == 403

    @pytest.mark.anyio
    async def test_rate_limited_returns_429(self) -> None:
        """Exceeding rate limit returns 429."""
        mw = _make_middleware()
        # Override the authorize rate limiter to a very low limit for testing
        mw._rate_limiters["/authorize"] = RateLimiter(max_requests=1, window_seconds=60)
        scope = {
            "type": "http",
            "path": "/authorize",
            "method": "GET",
            "query_string": b"response_type=code&client_id=abc&redirect_uri=https://x.com/cb",
            "headers": [],
            "client": ("1.1.1.1", 1234),
        }
        status1, _, _ = await _collect_response(mw, scope)
        assert status1 == 302
        status2, _, _ = await _collect_response(mw, scope)
        assert status2 == 429

    @pytest.mark.anyio
    async def test_upstream_timeout_returns_502(self) -> None:
        """Upstream timeout returns 502."""
        mw = _make_middleware()
        scope = {
            "type": "http",
            "path": "/token",
            "method": "POST",
            "headers": [(b"content-type", b"application/x-www-form-urlencoded")],
            "client": ("1.1.1.1", 1234),
        }
        with patch("urllib.request.urlopen", side_effect=Exception("timeout")):
            status, _, _ = await _collect_response(
                mw, scope, body=b"grant_type=authorization_code&code=abc"
            )
        assert status == 502

    @pytest.mark.anyio
    async def test_non_http_passthrough(self) -> None:
        """WebSocket and other non-HTTP scopes pass through."""
        mw = _make_middleware()
        scope = {
            "type": "websocket",
            "path": "/authorize",
            "headers": [],
            "client": ("1.1.1.1", 1234),
        }
        status, _body, _ = await _collect_response(mw, scope)
        assert status == 200

    @pytest.mark.anyio
    async def test_completed_flow_tracked(self) -> None:
        """A successful /token proxy increments completed_flows in stats."""
        mw = _make_middleware()
        scope = {
            "type": "http",
            "path": "/token",
            "method": "POST",
            "headers": [(b"content-type", b"application/x-www-form-urlencoded")],
            "client": ("1.1.1.1", 1234),
        }
        upstream_body = json.dumps({"access_token": "tok_123"}).encode()
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = upstream_body
        mock_resp.getheader.side_effect = lambda h, d=None: {
            "Content-Type": "application/json",
        }.get(h, d)
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_resp):
            await _collect_response(mw, scope, body=b"grant_type=authorization_code&code=abc")

        stats = mw.health_stats()
        assert stats["completed_flows"] == 1

    @pytest.mark.anyio
    async def test_health_stats_structure(self) -> None:
        """health_stats() returns the expected structure."""
        mw = _make_middleware()
        stats = mw.health_stats()
        assert "enabled" in stats
        assert "completed_flows" in stats
        assert "raw_hits" in stats
