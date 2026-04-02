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

from mcp_awareness.oauth_proxy import resolve_client_ip


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
