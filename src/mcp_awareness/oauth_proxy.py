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

"""OAuth proxy workaround for Claude Desktop/Claude.ai bugs.

Claude.ai ignores ``authorization_endpoint`` and ``token_endpoint`` from
OAuth metadata and instead constructs URLs from the MCP server's base URL.
This middleware intercepts those requests and forwards them to the real
OAuth provider (e.g. WorkOS AuthKit).

Feature-gated via ``AWARENESS_OAUTH_PROXY=true``.  Designed for removal
once the upstream bugs are fixed.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any

from starlette.types import Scope

logger = logging.getLogger(__name__)

# Default trusted-header chain — Cloudflare environment
_DEFAULT_IP_HEADERS = ["CF-Connecting-IP", "X-Real-IP"]


def resolve_client_ip(
    scope: Scope,
    *,
    ip_headers: list[str] | None = None,
) -> str:
    """Extract the real client IP from trusted proxy headers.

    Walks ``ip_headers`` in order (default: CF-Connecting-IP, X-Real-IP),
    returns the first non-empty value.  Falls back to the ASGI ``client``
    address if no trusted header is found.
    """
    chain = ip_headers if ip_headers is not None else _DEFAULT_IP_HEADERS
    raw_headers = dict(scope.get("headers", []))

    for header_name in chain:
        value = raw_headers.get(header_name.lower().encode(), b"").decode().strip()
        if value:
            return value

    client = scope.get("client")
    if client:
        return client[0]
    return "unknown"


class RateLimiter:
    """Per-IP sliding window rate limiter with temporary ban support."""

    def __init__(
        self,
        max_requests: int,
        window_seconds: int,
        ban_duration: int = 3600,
    ) -> None:
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self.ban_duration = ban_duration
        self._hits: dict[str, list[float]] = {}
        self._bans: dict[str, float] = {}  # ip -> ban_expiry (monotonic)
        self._rate_limited_count = 0
        self._last_rate_limited: str | None = None
        self._last_ban: str | None = None

    def check(self, ip: str) -> bool:
        """Return True if the request is allowed, False if rate-limited or banned."""
        now = time.monotonic()

        # Check ban first
        ban_expiry = self._bans.get(ip)
        if ban_expiry is not None:
            if now < ban_expiry:
                return False
            del self._bans[ip]

        # Sliding window
        timestamps = self._hits.get(ip, [])
        cutoff = now - self.window_seconds
        timestamps = [t for t in timestamps if t > cutoff]

        if len(timestamps) >= self.max_requests:
            self._hits[ip] = timestamps
            self._rate_limited_count += 1
            self._last_rate_limited = datetime.now(timezone.utc).isoformat()
            return False

        timestamps.append(now)
        self._hits[ip] = timestamps
        return True

    def ban(self, ip: str, *, reason: str) -> None:
        """Temporarily ban an IP."""
        self._bans[ip] = time.monotonic() + self.ban_duration
        self._last_ban = datetime.now(timezone.utc).isoformat()
        logger.warning("OAuth proxy: banned %s for %ds — %s", ip, self.ban_duration, reason)

    def stats(self) -> dict[str, Any]:
        """Return stats for the health endpoint."""
        now = time.monotonic()
        active_bans = sum(1 for exp in self._bans.values() if now < exp)
        return {
            "rate_limited": self._rate_limited_count,
            "last_rate_limited": self._last_rate_limited,
            "banned_ips": active_bans,
            "last_ban": self._last_ban,
        }
