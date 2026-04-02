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

import json
import logging
import re
import time
import urllib.request
from datetime import datetime, timezone
from typing import Any

from starlette.types import Scope

logger = logging.getLogger(__name__)

# Default trusted-header chain — Cloudflare environment
_DEFAULT_IP_HEADERS = ["CF-Connecting-IP", "X-Real-IP"]

# Patterns that indicate injection attempts in parameter values
_INJECTION_PATTERNS = re.compile(
    r"\.\./|\.\.\\|;.*(?:DROP|DELETE|INSERT|UPDATE|SELECT)|<script|%00|%0a|%0d",
    re.IGNORECASE,
)

# Required params per route and allowed methods
_ROUTE_RULES: dict[str, dict[str, Any]] = {
    "/authorize": {
        "method": "GET",
        "required_params": ["response_type", "client_id", "redirect_uri"],
    },
    "/token": {"method": "POST", "required_params": []},
    "/register": {"method": "POST", "required_params": []},
}


def detect_bogus_request(
    path: str,
    method: str,
    params: dict[str, str],
) -> str | None:
    """Detect unambiguously malicious requests.

    Returns a reason string if the request is bogus, None if it looks legitimate.
    """
    rule = _ROUTE_RULES.get(path)
    if rule is None:
        return None

    # Wrong HTTP method
    if method.upper() != rule["method"]:
        return f"Wrong method {method} for {path} (expected {rule['method']})"

    # Missing required params (only checked for routes that define them)
    for param in rule["required_params"]:
        if param not in params:
            return f"Missing required param '{param}' for {path}"

    # Injection patterns in any param value
    for key, value in params.items():
        if _INJECTION_PATTERNS.search(value):
            return f"Injection pattern detected in param '{key}'"

    return None


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
            return str(value)

    client: tuple[str, int] | None = scope.get("client")
    if client:
        return str(client[0])
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


class ProxyStats:
    """Tracks OAuth proxy traffic for the health endpoint."""

    def __init__(self) -> None:
        self._raw_hits: dict[str, int] = {"authorize": 0, "token": 0, "register": 0}
        self._completed_flows = 0
        self._last_completed_flow: str | None = None

    def record_hit(self, route: str) -> None:
        """Record a hit on a proxy route."""
        if route in self._raw_hits:
            self._raw_hits[route] += 1

    def record_completed_flow(self) -> None:
        """Record a successful token exchange (WorkOS returned 200 with access_token)."""
        self._completed_flows += 1
        self._last_completed_flow = datetime.now(timezone.utc).isoformat()

    def to_dict(self, rate_limiter_stats: dict[str, Any] | None = None) -> dict[str, Any]:
        """Build the health endpoint payload."""
        result: dict[str, Any] = {
            "enabled": True,
            "completed_flows": self._completed_flows,
            "last_completed_flow": self._last_completed_flow,
            "raw_hits": dict(self._raw_hits),
        }
        if rate_limiter_stats:
            result.update(rate_limiter_stats)
        return result


def discover_oidc_endpoints(issuer: str) -> dict[str, str | None] | None:
    """Discover OAuth endpoints from the issuer's OpenID configuration.

    Returns a dict with ``authorization_endpoint``, ``token_endpoint``,
    and ``registration_endpoint`` (may be None).  Returns None on failure.
    Endpoints are pinned at startup — never re-discovered from client input.
    """
    discovery_url = f"{issuer.rstrip('/')}/.well-known/openid-configuration"
    try:
        with urllib.request.urlopen(discovery_url, timeout=10) as resp:
            config = json.loads(resp.read())
    except Exception as exc:
        logger.error("OAuth proxy: OIDC discovery failed for %s: %s", discovery_url, exc)
        return None

    authorization_endpoint = config.get("authorization_endpoint")
    token_endpoint = config.get("token_endpoint")

    if not authorization_endpoint or not token_endpoint:
        logger.error(
            "OAuth proxy: OIDC config missing required endpoints "
            "(authorization_endpoint=%s, token_endpoint=%s)",
            authorization_endpoint,
            token_endpoint,
        )
        return None

    registration_endpoint = config.get("registration_endpoint")

    logger.info(
        "OAuth proxy: discovered endpoints — authorize=%s, token=%s, register=%s",
        authorization_endpoint,
        token_endpoint,
        registration_endpoint or "(not available)",
    )

    return {
        "authorization_endpoint": str(authorization_endpoint),
        "token_endpoint": str(token_endpoint),
        "registration_endpoint": str(registration_endpoint) if registration_endpoint else None,
    }
