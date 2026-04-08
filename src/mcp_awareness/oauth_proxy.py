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

import asyncio
import json
import logging
import re
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qs

from starlette.types import ASGIApp, Receive, Scope, Send

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


# ---------------------------------------------------------------------------
# OAuth Proxy ASGI Middleware
# ---------------------------------------------------------------------------

# Routes handled by the proxy
_OAUTH_ROUTES = {"/authorize", "/token", "/register"}

# Headers forwarded from upstream responses
_FORWARDED_HEADERS = {"Content-Type", "Cache-Control", "Set-Cookie", "WWW-Authenticate"}


class OAuthProxyMiddleware:
    """ASGI middleware that proxies OAuth routes to the real provider.

    Intercepts ``/authorize``, ``/token``, and ``/register`` and forwards
    them to the endpoints discovered via OIDC.  All other paths pass through
    to the wrapped ASGI application.
    """

    def __init__(
        self,
        app: ASGIApp,
        endpoints: dict[str, str | None],
        ban_duration: int = 3600,
        ip_headers: list[str] | None = None,
        rate_limits: dict[str, int] | None = None,
        rate_window: int = 60,
    ) -> None:
        self._app = app
        self._endpoints = endpoints
        self._ip_headers = ip_headers
        self._stats = ProxyStats()
        limits = rate_limits or {}
        self._rate_limiters: dict[str, RateLimiter] = {
            "/authorize": RateLimiter(
                max_requests=limits.get("/authorize", 60),
                window_seconds=rate_window,
                ban_duration=ban_duration,
            ),
            "/token": RateLimiter(
                max_requests=limits.get("/token", 60),
                window_seconds=rate_window,
                ban_duration=ban_duration,
            ),
            "/register": RateLimiter(
                max_requests=limits.get("/register", 30),
                window_seconds=rate_window,
                ban_duration=ban_duration,
            ),
        }
        self._ip_header_miss_count = 0
        self._ip_header_warned = False

        header_chain = ip_headers or _DEFAULT_IP_HEADERS
        logger.info(
            "OAuth proxy: IP header chain = %s",
            " → ".join(header_chain) + " → ASGI client",
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """ASGI entry point."""
        if scope.get("type") != "http":
            await self._app(scope, receive, send)
            return

        path: str = scope.get("path", "")
        method: str = scope.get("method", "GET").upper()

        if path not in _OAUTH_ROUTES:
            await self._app(scope, receive, send)
            return

        # CORS preflight for /token and /register
        if method == "OPTIONS" and path in ("/token", "/register"):
            await self._send_cors_preflight(send)
            return

        # Parse query params for bogus detection
        qs = scope.get("query_string", b"")
        params_multi = parse_qs(qs.decode() if isinstance(qs, bytes) else qs)
        params: dict[str, str] = {k: v[0] for k, v in params_multi.items()}

        # Resolve client IP and track header misses
        client_ip = resolve_client_ip(scope, ip_headers=self._ip_headers)
        self._check_ip_header_fallback(scope, client_ip)

        # Bogus request detection — ban and reject
        bogus_reason = detect_bogus_request(path, method, params)
        if bogus_reason is not None:
            limiter = self._rate_limiters.get(path)
            if limiter is not None:
                limiter.ban(client_ip, reason=bogus_reason)
            logger.warning(
                "OAuth proxy: bogus request from %s on %s — %s",
                client_ip,
                path,
                bogus_reason,
            )
            await self._send_json(send, 403, {"error": "forbidden"})
            return

        # Rate limiting
        limiter = self._rate_limiters.get(path)
        if limiter is not None and not limiter.check(client_ip):
            logger.info("OAuth proxy: rate-limited %s on %s", client_ip, path)
            await self._send_json(
                send,
                429,
                {"error": "rate_limited"},
                extra_headers=[(b"retry-after", b"60")],
            )
            return

        # Record hit
        route_name = path.lstrip("/")
        self._stats.record_hit(route_name)

        # Route to handler
        if path == "/authorize":
            await self._handle_authorize(scope, receive, send, params)
        elif path == "/token":
            upstream = self._endpoints.get("token_endpoint")
            if upstream:
                await self._handle_proxy(scope, receive, send, upstream, "token")
            else:
                await self._send_json(send, 404, {"error": "not_found"})
        elif path == "/register":
            upstream = self._endpoints.get("registration_endpoint")
            if upstream:
                await self._handle_proxy(scope, receive, send, upstream, "register")
            else:
                await self._send_json(send, 404, {"error": "not_found"})

    async def _handle_authorize(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
        params: dict[str, str],
    ) -> None:
        """Redirect GET /authorize to the real authorization endpoint."""
        auth_endpoint = self._endpoints.get("authorization_endpoint", "")
        qs = scope.get("query_string", b"")
        qs_str = qs.decode() if isinstance(qs, bytes) else qs
        redirect_url = f"{auth_endpoint}?{qs_str}" if qs_str else str(auth_endpoint)

        logger.info("OAuth proxy: redirecting /authorize → %s", auth_endpoint)
        await send(
            {
                "type": "http.response.start",
                "status": 302,
                "headers": [
                    (b"location", redirect_url.encode()),
                    (b"cache-control", b"no-store"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": b""})

    async def _handle_proxy(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
        upstream_url: str,
        route_name: str,
    ) -> None:
        """Proxy POST request to upstream and relay the response."""
        body = await self._read_body(receive)
        content_type = self._get_header(scope, b"content-type")

        try:
            status, resp_body, resp_headers = await asyncio.to_thread(
                self._do_proxy, upstream_url, body, content_type
            )
        except Exception as exc:
            logger.error(
                "OAuth proxy: upstream error for %s — %s: %s",
                route_name,
                type(exc).__name__,
                exc,
            )
            await self._send_json(send, 502, {"error": "upstream_error"})
            return

        # Track completed flows (successful token exchange)
        if route_name == "token" and status == 200:
            try:
                data = json.loads(resp_body)
                if "access_token" in data:
                    self._stats.record_completed_flow()
            except (json.JSONDecodeError, KeyError):
                pass

        # Build response headers
        response_headers: list[tuple[bytes, bytes]] = [
            (b"access-control-allow-origin", b"*"),
        ]
        for header_name in _FORWARDED_HEADERS:
            value = resp_headers.get(header_name)
            if value is not None:
                response_headers.append((header_name.lower().encode(), value.encode()))

        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": response_headers,
            }
        )
        await send({"type": "http.response.body", "body": resp_body})

    @staticmethod
    def _do_proxy(
        url: str, body: bytes, content_type: str | None
    ) -> tuple[int, bytes, dict[str, str]]:
        """Synchronous HTTP call to upstream (runs in a thread)."""
        req = urllib.request.Request(url, data=body, method="POST")
        if content_type:
            req.add_header("Content-Type", content_type)

        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                resp_body = resp.read()
                headers: dict[str, str] = {}
                for h in _FORWARDED_HEADERS:
                    val = resp.getheader(h)
                    if val is not None:
                        headers[h] = val
                return resp.status, resp_body, headers
        except urllib.error.HTTPError as exc:
            resp_body = exc.read() if exc.fp else b""
            headers = {}
            for h in _FORWARDED_HEADERS:
                val = exc.headers.get(h) if exc.headers else None
                if val is not None:
                    headers[h] = val
            return exc.code, resp_body, headers

    @staticmethod
    async def _read_body(receive: Receive) -> bytes:
        """Read the full ASGI request body."""
        body = b""
        while True:
            message = await receive()
            chunk: bytes = message.get("body", b"")
            body += chunk
            if not message.get("more_body", False):
                break
        return body

    @staticmethod
    def _get_header(scope: Scope, name: bytes) -> str | None:
        """Extract a single header value from the ASGI scope."""
        headers: list[tuple[bytes, bytes]] = scope.get("headers", [])
        for key, value in headers:
            if key == name:
                return value.decode()
        return None

    def _check_ip_header_fallback(self, scope: Scope, resolved_ip: str) -> None:
        """Warn if IP resolution is falling back to ASGI client address."""
        if self._ip_header_warned:
            return

        client: tuple[str, int] | None = scope.get("client")
        if client and resolved_ip == client[0]:
            self._ip_header_miss_count += 1
            if self._ip_header_miss_count >= 5:
                self._ip_header_warned = True
                header_chain = self._ip_headers or _DEFAULT_IP_HEADERS
                logger.warning(
                    "OAuth proxy: first %d requests fell back to ASGI client IP — "
                    "check that your reverse proxy sets %s",
                    self._ip_header_miss_count,
                    " or ".join(header_chain),
                )

    @staticmethod
    async def _send_cors_preflight(send: Send) -> None:
        """Send a 204 CORS preflight response."""
        await send(
            {
                "type": "http.response.start",
                "status": 204,
                "headers": [
                    (b"access-control-allow-origin", b"*"),
                    (b"access-control-allow-methods", b"POST, OPTIONS"),
                    (
                        b"access-control-allow-headers",
                        b"Content-Type, Authorization",
                    ),
                    (b"access-control-max-age", b"86400"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": b""})

    @staticmethod
    async def _send_json(
        send: Send,
        status: int,
        data: dict[str, Any],
        extra_headers: list[tuple[bytes, bytes]] | None = None,
    ) -> None:
        """Send a JSON response."""
        body = json.dumps(data).encode()
        headers: list[tuple[bytes, bytes]] = [
            (b"content-type", b"application/json"),
        ]
        if extra_headers:
            headers.extend(extra_headers)
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": headers,
            }
        )
        await send({"type": "http.response.body", "body": body})

    def health_stats(self) -> dict[str, Any]:
        """Return combined proxy and rate-limiter stats."""
        # Aggregate rate limiter stats across all routes
        total_rate_limited = 0
        total_banned = 0
        last_rate_limited: str | None = None
        last_ban: str | None = None

        per_route: dict[str, dict[str, Any]] = {}
        for route, limiter in self._rate_limiters.items():
            route_stats = limiter.stats()
            per_route[route] = route_stats
            total_rate_limited += route_stats["rate_limited"]
            total_banned += route_stats["banned_ips"]
            rl_ts = route_stats["last_rate_limited"]
            if rl_ts and (last_rate_limited is None or rl_ts > last_rate_limited):
                last_rate_limited = rl_ts
            ban_ts = route_stats["last_ban"]
            if ban_ts and (last_ban is None or ban_ts > last_ban):
                last_ban = ban_ts

        aggregated = {
            "rate_limited": total_rate_limited,
            "banned_ips": total_banned,
            "last_rate_limited": last_rate_limited,
            "last_ban": last_ban,
        }

        return self._stats.to_dict(rate_limiter_stats=aggregated)
