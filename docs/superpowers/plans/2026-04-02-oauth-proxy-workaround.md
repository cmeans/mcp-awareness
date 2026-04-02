# OAuth Proxy Workaround Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a feature-gated OAuth proxy middleware that forwards `/authorize`, `/token`, and `/register` requests to WorkOS, working around Claude Desktop/Claude.ai bugs that prevent OAuth with external identity providers.

**Architecture:** A single new module (`oauth_proxy.py`) containing an ASGI middleware, rate limiter, IP resolver, and health stats. The middleware slots into the existing ASGI chain in `server.py`. No changes to `middleware.py` or `oauth.py`. Uses `urllib.request` for upstream proxying (consistent with existing codebase).

**Tech Stack:** Python 3.11+, Starlette ASGI, urllib.request, pytest + anyio

**Spec:** `docs/superpowers/specs/2026-04-02-oauth-proxy-workaround-design.md`

---

## File Map

| File | Action | Responsibility |
|------|--------|----------------|
| `src/mcp_awareness/oauth_proxy.py` | Create | ASGI middleware, rate limiter, IP resolution, OIDC discovery, stats |
| `tests/test_oauth_proxy.py` | Create | All tests for the proxy module |
| `src/mcp_awareness/server.py` | Modify | Wire proxy into ASGI chain, add env vars, expose stats in health |

---

### Task 1: IP Resolution

The IP resolver extracts the real client IP from trusted proxy headers. Everything else (rate limiting, banning, logging) depends on it.

**Files:**
- Create: `tests/test_oauth_proxy.py`
- Create: `src/mcp_awareness/oauth_proxy.py`

- [ ] **Step 1: Write failing tests for IP resolution**

```python
# tests/test_oauth_proxy.py
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

import pytest

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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_oauth_proxy.py::TestResolveClientIp -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'mcp_awareness.oauth_proxy'`

- [ ] **Step 3: Implement IP resolution**

```python
# src/mcp_awareness/oauth_proxy.py
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_oauth_proxy.py::TestResolveClientIp -v`
Expected: All 5 tests PASS

- [ ] **Step 5: Commit**

```bash
git add src/mcp_awareness/oauth_proxy.py tests/test_oauth_proxy.py && git commit -m "feat(oauth-proxy): add IP resolution with configurable header chain"
```

---

### Task 2: Rate Limiter & Auto-Ban

Per-IP sliding window rate limiter with auto-ban for bogus requests. Pure logic — no ASGI integration yet.

**Files:**
- Modify: `tests/test_oauth_proxy.py`
- Modify: `src/mcp_awareness/oauth_proxy.py`

- [ ] **Step 1: Write failing tests for rate limiter**

Append to `tests/test_oauth_proxy.py`:

```python
import time
from unittest.mock import patch

from mcp_awareness.oauth_proxy import RateLimiter


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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_oauth_proxy.py::TestRateLimiter -v`
Expected: FAIL — `ImportError: cannot import name 'RateLimiter'`

- [ ] **Step 3: Implement rate limiter**

Add to `src/mcp_awareness/oauth_proxy.py`:

```python
import time
from datetime import datetime, timezone


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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_oauth_proxy.py::TestRateLimiter -v`
Expected: All 7 tests PASS

- [ ] **Step 5: Commit**

```bash
git add src/mcp_awareness/oauth_proxy.py tests/test_oauth_proxy.py && git commit -m "feat(oauth-proxy): add per-IP rate limiter with auto-ban"
```

---

### Task 3: Bogus Request Detection

Detect unambiguously malicious requests and trigger auto-bans. Pure logic — validates request params against OAuth requirements.

**Files:**
- Modify: `tests/test_oauth_proxy.py`
- Modify: `src/mcp_awareness/oauth_proxy.py`

- [ ] **Step 1: Write failing tests for bogus detection**

Append to `tests/test_oauth_proxy.py`:

```python
from mcp_awareness.oauth_proxy import detect_bogus_request


class TestDetectBogusRequest:
    """Tests for detect_bogus_request()."""

    def test_valid_authorize_passes(self) -> None:
        """A valid /authorize request is not flagged."""
        result = detect_bogus_request(
            "/authorize", "GET",
            {"response_type": "code", "client_id": "abc", "redirect_uri": "https://example.com/cb"},
        )
        assert result is None

    def test_authorize_missing_client_id(self) -> None:
        """Missing client_id on /authorize is flagged."""
        result = detect_bogus_request(
            "/authorize", "GET",
            {"response_type": "code", "redirect_uri": "https://example.com/cb"},
        )
        assert result is not None
        assert "client_id" in result

    def test_authorize_missing_response_type(self) -> None:
        """Missing response_type on /authorize is flagged."""
        result = detect_bogus_request(
            "/authorize", "GET",
            {"client_id": "abc", "redirect_uri": "https://example.com/cb"},
        )
        assert result is not None

    def test_authorize_missing_redirect_uri(self) -> None:
        """Missing redirect_uri on /authorize is flagged."""
        result = detect_bogus_request(
            "/authorize", "GET",
            {"response_type": "code", "client_id": "abc"},
        )
        assert result is not None

    def test_wrong_method_post_authorize(self) -> None:
        """POST to /authorize is flagged (should be GET)."""
        result = detect_bogus_request(
            "/authorize", "POST",
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
            "/authorize", "GET",
            {"response_type": "code", "client_id": "../../etc/passwd", "redirect_uri": "https://x.com/cb"},
        )
        assert result is not None
        assert "injection" in result.lower()

    def test_sql_injection_in_params(self) -> None:
        """SQL injection pattern in param values is flagged."""
        result = detect_bogus_request(
            "/authorize", "GET",
            {"response_type": "code", "client_id": "'; DROP TABLE users;--", "redirect_uri": "https://x.com/cb"},
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_oauth_proxy.py::TestDetectBogusRequest -v`
Expected: FAIL — `ImportError: cannot import name 'detect_bogus_request'`

- [ ] **Step 3: Implement bogus detection**

Add to `src/mcp_awareness/oauth_proxy.py`:

```python
import re

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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_oauth_proxy.py::TestDetectBogusRequest -v`
Expected: All 10 tests PASS

- [ ] **Step 5: Commit**

```bash
git add src/mcp_awareness/oauth_proxy.py tests/test_oauth_proxy.py && git commit -m "feat(oauth-proxy): add bogus request detection for auto-ban"
```

---

### Task 4: OIDC Endpoint Discovery

Discover the real authorization, token, and registration endpoints from the issuer's OpenID configuration. Endpoints are pinned at init time.

**Files:**
- Modify: `tests/test_oauth_proxy.py`
- Modify: `src/mcp_awareness/oauth_proxy.py`

- [ ] **Step 1: Write failing tests for OIDC discovery**

Append to `tests/test_oauth_proxy.py`:

```python
import json
from unittest.mock import patch, MagicMock

from mcp_awareness.oauth_proxy import discover_oidc_endpoints


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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_oauth_proxy.py::TestDiscoverOidcEndpoints -v`
Expected: FAIL — `ImportError: cannot import name 'discover_oidc_endpoints'`

- [ ] **Step 3: Implement OIDC discovery**

Add to `src/mcp_awareness/oauth_proxy.py`:

```python
import json
import urllib.request


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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_oauth_proxy.py::TestDiscoverOidcEndpoints -v`
Expected: All 4 tests PASS

- [ ] **Step 5: Commit**

```bash
git add src/mcp_awareness/oauth_proxy.py tests/test_oauth_proxy.py && git commit -m "feat(oauth-proxy): add OIDC endpoint discovery"
```

---

### Task 5: Proxy Stats Tracker

Centralized stats object that tracks raw hits, completed flows, rate limits, and bans for the health endpoint.

**Files:**
- Modify: `tests/test_oauth_proxy.py`
- Modify: `src/mcp_awareness/oauth_proxy.py`

- [ ] **Step 1: Write failing tests for stats tracker**

Append to `tests/test_oauth_proxy.py`:

```python
from mcp_awareness.oauth_proxy import ProxyStats


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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_oauth_proxy.py::TestProxyStats -v`
Expected: FAIL — `ImportError: cannot import name 'ProxyStats'`

- [ ] **Step 3: Implement stats tracker**

Add to `src/mcp_awareness/oauth_proxy.py`:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_oauth_proxy.py::TestProxyStats -v`
Expected: All 3 tests PASS

- [ ] **Step 5: Commit**

```bash
git add src/mcp_awareness/oauth_proxy.py tests/test_oauth_proxy.py && git commit -m "feat(oauth-proxy): add proxy stats tracker for health endpoint"
```

---

### Task 6: ASGI Middleware — Route Handling

The core middleware class that ties everything together: intercepts OAuth routes, handles CORS, redirects `/authorize`, proxies `/token` and `/register`, and applies rate limiting + bogus detection.

**Files:**
- Modify: `tests/test_oauth_proxy.py`
- Modify: `src/mcp_awareness/oauth_proxy.py`

- [ ] **Step 1: Write failing tests for the middleware**

Append to `tests/test_oauth_proxy.py`:

```python
from mcp_awareness.oauth_proxy import OAuthProxyMiddleware


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
            "type": "http", "path": "/mcp", "method": "POST",
            "headers": [], "client": ("1.1.1.1", 1234),
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
            "query_string": b"response_type=code&client_id=abc&redirect_uri=https%3A%2F%2Fexample.com%2Fcb&state=xyz",
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
            "type": "http", "path": "/token", "method": "POST",
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
            status, body, headers = await _collect_response(
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
            "type": "http", "path": "/token", "method": "OPTIONS",
            "headers": [], "client": ("1.1.1.1", 1234),
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
            "type": "http", "path": "/register", "method": "OPTIONS",
            "headers": [], "client": ("1.1.1.1", 1234),
        }
        status, _, headers = await _collect_response(mw, scope)
        assert status == 204

    @pytest.mark.anyio
    async def test_register_404_when_not_discovered(self) -> None:
        """POST /register returns 404 when registration_endpoint was not discovered."""
        mw = _make_middleware(endpoints={
            "authorization_endpoint": "https://auth.example.com/authorize",
            "token_endpoint": "https://auth.example.com/token",
            "registration_endpoint": None,
        })
        scope = {
            "type": "http", "path": "/register", "method": "POST",
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
            "type": "http", "path": "/token", "method": "POST",
            "headers": [(b"content-type", b"application/x-www-form-urlencoded")],
            "client": ("1.1.1.1", 1234),
        }
        with patch("urllib.request.urlopen", side_effect=Exception("timeout")):
            status, _, _ = await _collect_response(mw, scope, body=b"grant_type=authorization_code&code=abc")
        assert status == 502

    @pytest.mark.anyio
    async def test_non_http_passthrough(self) -> None:
        """WebSocket and other non-HTTP scopes pass through."""
        mw = _make_middleware()
        scope = {"type": "websocket", "path": "/authorize", "headers": [], "client": ("1.1.1.1", 1234)}
        status, body, _ = await _collect_response(mw, scope)
        # _dummy_app doesn't handle websockets properly, but the key assertion
        # is that the middleware passes through without intercepting
        assert status == 200

    @pytest.mark.anyio
    async def test_completed_flow_tracked(self) -> None:
        """A successful /token proxy increments completed_flows in stats."""
        mw = _make_middleware()
        scope = {
            "type": "http", "path": "/token", "method": "POST",
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
        assert "rate_limited" in stats or "banned_ips" in stats
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_oauth_proxy.py::TestOAuthProxyMiddleware -v`
Expected: FAIL — `ImportError: cannot import name 'OAuthProxyMiddleware'`

- [ ] **Step 3: Implement the ASGI middleware**

Add to `src/mcp_awareness/oauth_proxy.py`:

```python
import asyncio
from urllib.parse import urlencode, parse_qs

from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send


class OAuthProxyMiddleware:
    """ASGI middleware that proxies OAuth routes to an external provider.

    Intercepts /authorize, /token, and /register, forwarding them to the
    real OAuth endpoints discovered from OIDC configuration.  All other
    requests pass through to the inner app.
    """

    def __init__(
        self,
        app: ASGIApp,
        endpoints: dict[str, str | None],
        ban_duration: int = 3600,
        ip_headers: list[str] | None = None,
    ) -> None:
        self.app = app
        self._endpoints = endpoints
        self._ip_headers = ip_headers or list(_DEFAULT_IP_HEADERS)
        self._stats = ProxyStats()

        # Per-route rate limiters
        self._rate_limiters: dict[str, RateLimiter] = {
            "/authorize": RateLimiter(max_requests=20, window_seconds=60, ban_duration=ban_duration),
            "/token": RateLimiter(max_requests=10, window_seconds=60, ban_duration=ban_duration),
            "/register": RateLimiter(max_requests=5, window_seconds=60, ban_duration=ban_duration),
        }

        self._ip_header_warning_logged = False
        self._requests_without_headers = 0

        logger.info(
            "OAuth proxy: IP resolution chain = %s",
            self._ip_headers + ["asgi-client"],
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path: str = scope.get("path", "")

        if path not in ("/authorize", "/token", "/register"):
            await self.app(scope, receive, send)
            return

        method: str = scope.get("method", "GET").upper()

        # CORS preflight
        if method == "OPTIONS" and path in ("/token", "/register"):
            resp = Response(status_code=204, headers={
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Methods": "POST, OPTIONS",
                "Access-Control-Allow-Headers": "Content-Type, Authorization",
                "Access-Control-Max-Age": "86400",
            })
            await resp(scope, receive, send)
            return

        # Resolve client IP
        client_ip = resolve_client_ip(scope, ip_headers=self._ip_headers)

        # Track IP header availability for warning
        if client_ip == scope.get("client", (None,))[0]:
            self._requests_without_headers += 1
            if self._requests_without_headers >= 5 and not self._ip_header_warning_logged:
                logger.warning(
                    "OAuth proxy: none of the configured IP headers (%s) found in "
                    "the first %d requests — rate limiting uses ASGI client address. "
                    "Update AWARENESS_OAUTH_PROXY_IP_HEADERS if infra changed.",
                    self._ip_headers,
                    self._requests_without_headers,
                )
                self._ip_header_warning_logged = True

        # Parse query params for bogus detection
        query_string = scope.get("query_string", b"").decode()
        params = {k: v[0] for k, v in parse_qs(query_string).items()} if query_string else {}

        # Bogus request detection → auto-ban
        bogus_reason = detect_bogus_request(path, method, params)
        if bogus_reason:
            rl = self._rate_limiters.get(path)
            if rl:
                rl.ban(client_ip, reason=bogus_reason)
            resp = JSONResponse({"error": "Forbidden"}, status_code=403)
            await resp(scope, receive, send)
            return

        # Rate limiting
        rl = self._rate_limiters.get(path)
        if rl and not rl.check(client_ip):
            resp = Response("Too Many Requests", status_code=429, headers={"Retry-After": "60"})
            await resp(scope, receive, send)
            return

        # Record hit
        route_name = path.lstrip("/")
        self._stats.record_hit(route_name)

        # Route handling
        if path == "/authorize":
            await self._handle_authorize(scope, receive, send, params)
        elif path == "/token":
            await self._handle_proxy(scope, receive, send, self._endpoints["token_endpoint"], route_name)
        elif path == "/register":
            reg_endpoint = self._endpoints.get("registration_endpoint")
            if not reg_endpoint:
                resp = JSONResponse({"error": "Registration not available"}, status_code=404)
                await resp(scope, receive, send)
                return
            await self._handle_proxy(scope, receive, send, reg_endpoint, route_name)

    async def _handle_authorize(
        self, scope: Scope, receive: Receive, send: Send, params: dict[str, str]
    ) -> None:
        """Redirect to the real authorization endpoint with all query params."""
        auth_endpoint = self._endpoints["authorization_endpoint"]
        query_string = scope.get("query_string", b"").decode()
        redirect_url = f"{auth_endpoint}?{query_string}" if query_string else auth_endpoint
        resp = Response(status_code=302, headers={"Location": redirect_url})
        await resp(scope, receive, send)

    async def _handle_proxy(
        self, scope: Scope, receive: Receive, send: Send, upstream_url: str | None, route_name: str
    ) -> None:
        """Proxy a POST request to the upstream endpoint and relay the response."""
        if not upstream_url:
            resp = JSONResponse({"error": "Endpoint not available"}, status_code=404)
            await resp(scope, receive, send)
            return

        # Read request body
        request_body = await self._read_body(receive)

        # Extract Content-Type from request headers
        raw_headers = dict(scope.get("headers", []))
        content_type = raw_headers.get(b"content-type", b"application/x-www-form-urlencoded").decode()

        # Proxy to upstream in a thread to avoid blocking the event loop
        try:
            upstream_status, upstream_body, upstream_headers = await asyncio.to_thread(
                self._do_proxy, upstream_url, request_body, content_type
            )
        except Exception as exc:
            logger.warning("OAuth proxy: upstream request to %s failed: %s", upstream_url, exc)
            resp = JSONResponse({"error": "Bad Gateway"}, status_code=502)
            await resp(scope, receive, send)
            return

        # Track completed flows (successful token exchange)
        if route_name == "token" and upstream_status == 200:
            try:
                resp_data = json.loads(upstream_body)
                if "access_token" in resp_data:
                    self._stats.record_completed_flow()
            except (json.JSONDecodeError, KeyError):
                pass

        # Build response headers
        resp_headers: dict[str, str] = {"Access-Control-Allow-Origin": "*"}
        for header_name in ("Content-Type", "Cache-Control", "Set-Cookie", "WWW-Authenticate"):
            value = upstream_headers.get(header_name)
            if value:
                resp_headers[header_name] = value

        resp = Response(content=upstream_body, status_code=upstream_status, headers=resp_headers)
        await resp(scope, receive, send)

    @staticmethod
    def _do_proxy(
        url: str, body: bytes, content_type: str
    ) -> tuple[int, bytes, dict[str, str | None]]:
        """Synchronous HTTP proxy call — runs in a thread."""
        req = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={"Content-Type": content_type},
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                resp_body = resp.read()
                headers = {
                    h: resp.getheader(h) for h in
                    ("Content-Type", "Cache-Control", "Set-Cookie", "WWW-Authenticate")
                }
                return resp.status, resp_body, headers
        except urllib.error.HTTPError as exc:
            resp_body = exc.read()
            headers = {
                h: exc.headers.get(h) for h in
                ("Content-Type", "Cache-Control", "Set-Cookie", "WWW-Authenticate")
            }
            return exc.code, resp_body, headers

    @staticmethod
    async def _read_body(receive: Receive) -> bytes:
        """Read the full ASGI request body."""
        body = b""
        while True:
            message = await receive()
            body += message.get("body", b"")
            if not message.get("more_body", False):
                break
        return body

    def health_stats(self) -> dict[str, Any]:
        """Return stats for the /health endpoint."""
        # Merge rate limiter stats
        rl_stats: dict[str, Any] = {
            "rate_limited": {},
            "banned_ips": 0,
            "last_rate_limited": None,
            "last_ban": None,
        }
        for route, limiter in self._rate_limiters.items():
            ls = limiter.stats()
            rl_stats["rate_limited"][route.lstrip("/")] = ls["rate_limited"]
            rl_stats["banned_ips"] += ls["banned_ips"]
            if ls["last_rate_limited"]:
                if not rl_stats["last_rate_limited"] or ls["last_rate_limited"] > rl_stats["last_rate_limited"]:
                    rl_stats["last_rate_limited"] = ls["last_rate_limited"]
            if ls["last_ban"]:
                if not rl_stats["last_ban"] or ls["last_ban"] > rl_stats["last_ban"]:
                    rl_stats["last_ban"] = ls["last_ban"]

        return self._stats.to_dict(rate_limiter_stats=rl_stats)
```

Also add the missing import at the top of the file (near existing imports):

```python
import urllib.error
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_oauth_proxy.py::TestOAuthProxyMiddleware -v`
Expected: All 12 tests PASS

- [ ] **Step 5: Run all proxy tests together**

Run: `python -m pytest tests/test_oauth_proxy.py -v`
Expected: All tests PASS (IP resolution + rate limiter + bogus detection + OIDC discovery + stats + middleware)

- [ ] **Step 6: Commit**

```bash
git add src/mcp_awareness/oauth_proxy.py tests/test_oauth_proxy.py && git commit -m "feat(oauth-proxy): add OAuthProxyMiddleware with route handling, CORS, and stats"
```

---

### Task 7: Server Wiring

Wire the proxy middleware into the ASGI chain in `server.py`, add environment variables, and expose proxy stats in the health endpoint.

**Files:**
- Modify: `src/mcp_awareness/server.py`
- Modify: `tests/test_oauth_proxy.py`

- [ ] **Step 1: Write failing tests for server wiring**

Append to `tests/test_oauth_proxy.py`:

```python
from mcp_awareness import server as server_mod


class TestServerWiring:
    """Tests for OAuth proxy env var handling and health integration."""

    def test_oauth_proxy_disabled_by_default(self) -> None:
        """AWARENESS_OAUTH_PROXY defaults to false."""
        assert server_mod.OAUTH_PROXY is False or not hasattr(server_mod, "OAUTH_PROXY")

    @patch.dict("os.environ", {"AWARENESS_OAUTH_PROXY": "true", "AWARENESS_OAUTH_ISSUER": "https://auth.example.com"})
    def test_oauth_proxy_env_var_enables(self) -> None:
        """AWARENESS_OAUTH_PROXY=true is read from environment."""
        # Re-evaluate — env vars are read at import time, so we check the
        # config function instead
        from importlib import reload
        with patch("mcp_awareness.oauth_proxy.discover_oidc_endpoints", return_value={
            "authorization_endpoint": "https://auth.example.com/authorize",
            "token_endpoint": "https://auth.example.com/token",
            "registration_endpoint": None,
        }):
            reload(server_mod)
            assert server_mod.OAUTH_PROXY is True
        # Restore
        with patch.dict("os.environ", {}, clear=False):
            reload(server_mod)

    def test_health_response_no_proxy_stats(self) -> None:
        """Health response omits oauth_proxy when proxy is disabled."""
        health = server_mod._health_response()
        assert "oauth_proxy" not in health
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_oauth_proxy.py::TestServerWiring -v`
Expected: FAIL — `AttributeError: module 'mcp_awareness.server' has no attribute 'OAUTH_PROXY'`

- [ ] **Step 3: Add env vars and health integration to server.py**

In `src/mcp_awareness/server.py`, add below the `PUBLIC_URL` line (around line 89):

```python
# OAuth proxy workaround — feature-gated, see docs/superpowers/specs/2026-04-02-oauth-proxy-workaround-design.md
OAUTH_PROXY = os.environ.get("AWARENESS_OAUTH_PROXY", "false").lower() == "true"
OAUTH_PROXY_BAN_DURATION = int(os.environ.get("AWARENESS_OAUTH_PROXY_BAN_DURATION", "3600"))
OAUTH_PROXY_IP_HEADERS = [
    h.strip()
    for h in os.environ.get("AWARENESS_OAUTH_PROXY_IP_HEADERS", "CF-Connecting-IP,X-Real-IP").split(",")
    if h.strip()
]
```

Add a module-level variable to hold the proxy middleware instance (below `store`):

```python
# OAuth proxy middleware instance (set during _run() if enabled)
_oauth_proxy: Any = None
```

Update `_health_response()` to include proxy stats:

```python
def _health_response() -> dict[str, Any]:
    """Build the health check response payload."""
    result: dict[str, Any] = {
        "status": "ok",
        "uptime_sec": round(time.monotonic() - _start_time, 1),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "transport": TRANSPORT,
    }
    if _oauth_proxy is not None:
        result["oauth_proxy"] = _oauth_proxy.health_stats()
    return result
```

Update the `elif TRANSPORT == "streamable-http":` branch (no MOUNT_PATH — line 443) to insert the proxy:

```python
    elif TRANSPORT == "streamable-http":
        import uvicorn

        from mcp_awareness.middleware import HealthMiddleware

        inner_app = mcp.streamable_http_app()
        health_app: Any = HealthMiddleware(inner_app, _health_response)

        if OAUTH_ISSUER:
            from mcp_awareness.middleware import WellKnownMiddleware

            health_app = WellKnownMiddleware(
                health_app,
                OAUTH_ISSUER,
                public_url=PUBLIC_URL,
                host=HOST,
                port=PORT,
                mount_path=MOUNT_PATH,
            )

        health_app = _wrap_with_auth(health_app)

        # OAuth proxy workaround — slots outside AuthMiddleware
        if OAUTH_PROXY and OAUTH_ISSUER:
            global _oauth_proxy
            from mcp_awareness.oauth_proxy import OAuthProxyMiddleware, discover_oidc_endpoints

            endpoints = discover_oidc_endpoints(OAUTH_ISSUER)
            if endpoints:
                _oauth_proxy = OAuthProxyMiddleware(
                    health_app,
                    endpoints=endpoints,
                    ban_duration=OAUTH_PROXY_BAN_DURATION,
                    ip_headers=OAUTH_PROXY_IP_HEADERS,
                )
                health_app = _oauth_proxy
                logger.info("OAuth proxy: enabled — intercepting /authorize, /token, /register")
            else:
                logger.error("OAuth proxy: OIDC discovery failed — proxy disabled")

        from starlette.middleware.gzip import GZipMiddleware

        health_app = GZipMiddleware(health_app, minimum_size=500)

        config = uvicorn.Config(health_app, host=HOST, port=PORT)
        server = uvicorn.Server(config)

        import anyio

        anyio.run(server.serve)
```

Also update the `TRANSPORT == "streamable-http" and MOUNT_PATH` branch (line 409) with the same proxy wiring after `_wrap_with_auth(app)`:

```python
        app = _wrap_with_auth(app)

        # OAuth proxy workaround — slots outside AuthMiddleware
        if OAUTH_PROXY and OAUTH_ISSUER:
            global _oauth_proxy
            from mcp_awareness.oauth_proxy import OAuthProxyMiddleware, discover_oidc_endpoints

            endpoints = discover_oidc_endpoints(OAUTH_ISSUER)
            if endpoints:
                _oauth_proxy = OAuthProxyMiddleware(
                    app,
                    endpoints=endpoints,
                    ban_duration=OAUTH_PROXY_BAN_DURATION,
                    ip_headers=OAUTH_PROXY_IP_HEADERS,
                )
                app = _oauth_proxy
                logger.info("OAuth proxy: enabled — intercepting /authorize, /token, /register")
            else:
                logger.error("OAuth proxy: OIDC discovery failed — proxy disabled")

        from starlette.middleware.gzip import GZipMiddleware
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_oauth_proxy.py::TestServerWiring -v`
Expected: All 3 tests PASS

- [ ] **Step 5: Run the full test suite**

Run: `python -m pytest tests/test_oauth_proxy.py -v`
Expected: All tests PASS

- [ ] **Step 6: Run linting and type checking**

Run: `ruff check src/mcp_awareness/oauth_proxy.py tests/test_oauth_proxy.py && ruff format --check src/mcp_awareness/oauth_proxy.py tests/test_oauth_proxy.py && mypy src/mcp_awareness/oauth_proxy.py`
Expected: All clean

- [ ] **Step 7: Commit**

```bash
git add src/mcp_awareness/server.py src/mcp_awareness/oauth_proxy.py tests/test_oauth_proxy.py && git commit -m "feat(oauth-proxy): wire middleware into ASGI chain with env vars and health stats"
```

---

### Task 8: Documentation Updates

Update CHANGELOG, README, and docker-compose to document the new feature and env vars.

**Files:**
- Modify: `CHANGELOG.md`
- Modify: `README.md`
- Modify: `docker-compose.yaml`

- [ ] **Step 1: Update CHANGELOG.md**

Add under `[Unreleased]`:

```markdown
### Added
- **OAuth proxy workaround**: feature-gated middleware (`AWARENESS_OAUTH_PROXY=true`) that proxies `/authorize`, `/token`, `/register` to the external OAuth provider (e.g. WorkOS) — works around Claude Desktop/Claude.ai bugs that ignore external auth endpoints (#82, #125)
- **OAuth proxy rate limiting**: per-IP sliding window rate limits with auto-ban for bogus requests (injection patterns, wrong HTTP methods, missing required params)
- **OAuth proxy health stats**: `/health` endpoint includes `oauth_proxy` section with completed flows, raw hits, rate-limited counts, and banned IP counts — enables detection of when upstream bugs are fixed
- **Configurable IP resolution**: `AWARENESS_OAUTH_PROXY_IP_HEADERS` env var for infrastructure-portable client IP detection (default: `CF-Connecting-IP,X-Real-IP`)
```

- [ ] **Step 2: Update README.md env var table**

Add to the environment variables table:

```markdown
| `AWARENESS_OAUTH_PROXY` | `false` | Enable OAuth proxy workaround for Claude Desktop/Claude.ai |
| `AWARENESS_OAUTH_PROXY_BAN_DURATION` | `3600` | Auto-ban duration (seconds) for bogus OAuth requests |
| `AWARENESS_OAUTH_PROXY_IP_HEADERS` | `CF-Connecting-IP,X-Real-IP` | Trusted IP header priority chain |
```

- [ ] **Step 3: Update docker-compose.yaml**

Add the proxy env vars to the service environment section (passthrough from `.env`):

```yaml
      - AWARENESS_OAUTH_PROXY=${AWARENESS_OAUTH_PROXY:-false}
      - AWARENESS_OAUTH_PROXY_BAN_DURATION=${AWARENESS_OAUTH_PROXY_BAN_DURATION:-3600}
      - AWARENESS_OAUTH_PROXY_IP_HEADERS=${AWARENESS_OAUTH_PROXY_IP_HEADERS:-CF-Connecting-IP,X-Real-IP}
```

- [ ] **Step 4: Update test count in README if needed**

Check the total test count after all new tests:

Run: `python -m pytest tests/ --co -q | tail -1`

Update the test count in README.md to match.

- [ ] **Step 5: Commit**

```bash
git add CHANGELOG.md README.md docker-compose.yaml && git commit -m "docs: add OAuth proxy workaround to changelog, readme, and docker-compose"
```

---

### Task 9: Final Verification

Run the full test suite, linter, type checker, and formatter to confirm everything is clean.

**Files:** (no changes — verification only)

- [ ] **Step 1: Run full test suite**

Run: `python -m pytest tests/ -v`
Expected: All tests PASS (existing + new OAuth proxy tests)

- [ ] **Step 2: Run linting**

Run: `ruff check src/ tests/`
Expected: No errors

- [ ] **Step 3: Run formatter check**

Run: `ruff format --check src/ tests/`
Expected: No reformatting needed

- [ ] **Step 4: Run type checker**

Run: `mypy src/mcp_awareness/`
Expected: No errors

- [ ] **Step 5: Verify test count matches README**

Run: `python -m pytest tests/ --co -q | tail -1`
Confirm the number matches what's in README.md.
