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

import time
from unittest.mock import patch

from mcp_awareness.oauth_proxy import RateLimiter, detect_bogus_request, resolve_client_ip


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
