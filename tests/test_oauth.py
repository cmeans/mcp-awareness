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

"""Tests for OAuth 2.1 resource server (JWKS validation, auto-provisioning, metadata)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import MagicMock

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
)

from mcp_awareness.middleware import AuthMiddleware, WellKnownMiddleware
from mcp_awareness.oauth import OAuthTokenValidator

# ---------------------------------------------------------------------------
# RSA key pair for testing
# ---------------------------------------------------------------------------

_TEST_PRIVATE_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_TEST_PUBLIC_KEY = _TEST_PRIVATE_KEY.public_key()

TEST_ISSUER = "https://auth.example.com"
TEST_AUDIENCE = "awareness-test"
TEST_OWNER = "test-owner"


def _make_token(
    sub: str = TEST_OWNER,
    issuer: str = TEST_ISSUER,
    audience: str = TEST_AUDIENCE,
    email: str | None = None,
    name: str | None = None,
    expired: bool = False,
) -> str:
    """Create a signed RS256 JWT for testing."""
    now = datetime.now(timezone.utc)
    payload: dict[str, Any] = {
        "sub": sub,
        "iss": issuer,
        "aud": audience,
        "iat": now,
        "exp": now + timedelta(hours=-1 if expired else 1),
    }
    if email:
        payload["email"] = email
    if name:
        payload["name"] = name

    private_pem = _TEST_PRIVATE_KEY.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())
    return jwt.encode(payload, private_pem, algorithm="RS256")


# ---------------------------------------------------------------------------
# OAuthTokenValidator tests
# ---------------------------------------------------------------------------


class TestOAuthTokenValidator:
    def _make_validator(self, audience: str = TEST_AUDIENCE) -> OAuthTokenValidator:
        validator = OAuthTokenValidator(
            issuer=TEST_ISSUER,
            audience=audience,
            user_claim="sub",
        )
        return validator

    def _mock_jwk_client(self, validator: OAuthTokenValidator) -> None:
        """Replace the PyJWKClient with a mock that returns our test key."""
        import time as _time

        mock_client = MagicMock()
        mock_signing_key = MagicMock()
        mock_signing_key.key = _TEST_PUBLIC_KEY
        mock_client.get_signing_key_from_jwt.return_value = mock_signing_key
        validator._jwk_client = mock_client
        # Prevent validate() from replacing our mock with a real client
        validator._last_jwks_fetch = _time.monotonic()

    def test_valid_token(self) -> None:
        validator = self._make_validator()
        self._mock_jwk_client(validator)
        token = _make_token()
        result = validator.validate(token)
        assert result["owner_id"] == TEST_OWNER

    def test_token_with_email_and_name(self) -> None:
        validator = self._make_validator()
        self._mock_jwk_client(validator)
        token = _make_token(email="alice@example.com", name="Alice")
        result = validator.validate(token)
        assert result["owner_id"] == TEST_OWNER
        assert result["email"] == "alice@example.com"
        assert result["name"] == "Alice"

    def test_expired_token_raises(self) -> None:
        validator = self._make_validator()
        self._mock_jwk_client(validator)
        token = _make_token(expired=True)
        with pytest.raises(jwt.ExpiredSignatureError):
            validator.validate(token)

    def test_wrong_issuer_raises(self) -> None:
        validator = self._make_validator()
        self._mock_jwk_client(validator)
        token = _make_token(issuer="https://wrong-issuer.com")
        with pytest.raises(jwt.InvalidIssuerError):
            validator.validate(token)

    def test_wrong_audience_raises(self) -> None:
        validator = self._make_validator(audience="wrong-audience")
        self._mock_jwk_client(validator)
        token = _make_token()
        with pytest.raises(jwt.InvalidAudienceError):
            validator.validate(token)

    def test_missing_sub_claim_raises(self) -> None:
        validator = self._make_validator()
        self._mock_jwk_client(validator)
        # Create a token without sub
        now = datetime.now(timezone.utc)
        payload = {
            "iss": TEST_ISSUER,
            "aud": TEST_AUDIENCE,
            "iat": now,
            "exp": now + timedelta(hours=1),
        }
        private_pem = _TEST_PRIVATE_KEY.private_bytes(
            Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()
        )
        token = jwt.encode(payload, private_pem, algorithm="RS256")
        with pytest.raises(jwt.InvalidTokenError, match="missing 'sub' claim"):
            validator.validate(token)

    def test_custom_user_claim(self) -> None:
        validator = OAuthTokenValidator(
            issuer=TEST_ISSUER,
            audience=TEST_AUDIENCE,
            user_claim="email",
        )
        self._mock_jwk_client(validator)
        token = _make_token(email="alice@example.com")
        result = validator.validate(token)
        assert result["owner_id"] == "alice@example.com"

    def test_explicit_jwks_uri(self) -> None:
        """Explicit jwks_uri overrides the default derived from issuer."""
        validator = OAuthTokenValidator(
            issuer=TEST_ISSUER,
            audience=TEST_AUDIENCE,
            jwks_uri="https://custom.example.com/keys",
            user_claim="sub",
        )
        assert validator._jwks_uri == "https://custom.example.com/keys"

    def test_jwks_cache_refresh(self) -> None:
        """JWKS client is refreshed when cache TTL expires."""
        validator = self._make_validator()
        self._mock_jwk_client(validator)
        # Force cache to be expired
        validator._last_jwks_fetch = 0.0
        validator._jwks_cache_ttl = 0  # instant expiry
        token = _make_token()
        # This triggers cache refresh (creates new PyJWKClient) which fails
        # because it tries to fetch from a fake URL. That's expected.
        with pytest.raises(jwt.exceptions.PyJWKClientConnectionError):
            validator.validate(token)

    def test_jwks_lock_prevents_concurrent_refresh(self) -> None:
        """Double-check pattern: second thread sees refreshed cache after lock."""
        validator = self._make_validator()
        self._mock_jwk_client(validator)
        # Force cache expired
        validator._last_jwks_fetch = 0.0
        validator._jwks_cache_ttl = 0

        # Simulate a refresh by updating _last_jwks_fetch (as if another thread did it)
        import time as _time

        validator._last_jwks_fetch = _time.monotonic() + 9999
        # Now validate should NOT refresh (double-check passes)
        token = _make_token()
        result = validator.validate(token)
        assert result["owner_id"] == TEST_OWNER
        # Verify the lock attribute exists
        assert hasattr(validator, "_jwks_lock")

    def test_no_audience_skips_validation(self) -> None:
        validator = OAuthTokenValidator(
            issuer=TEST_ISSUER,
            audience="",
            user_claim="sub",
        )
        self._mock_jwk_client(validator)
        token = _make_token()
        result = validator.validate(token)
        assert result["owner_id"] == TEST_OWNER

    def test_explicit_jwks_uri_skips_discovery(self) -> None:
        """When jwks_uri is provided, OIDC discovery should not be attempted."""
        from unittest.mock import patch

        explicit_uri = "https://auth.example.com/custom/jwks"
        with patch("mcp_awareness.oauth.urllib.request.urlopen") as mock_urlopen:
            validator = OAuthTokenValidator(
                issuer=TEST_ISSUER,
                audience=TEST_AUDIENCE,
                jwks_uri=explicit_uri,
            )
            mock_urlopen.assert_not_called()
        assert validator._jwks_uri == explicit_uri

    def test_oidc_discovery_extracts_jwks_uri(self) -> None:
        """When jwks_uri is empty, auto-discover from OIDC configuration."""
        from unittest.mock import patch

        discovered_uri = "https://auth.example.com/oauth2/jwks"
        oidc_config = json.dumps({"jwks_uri": discovered_uri}).encode()

        mock_resp = MagicMock()
        mock_resp.read.return_value = oidc_config
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("mcp_awareness.oauth.urllib.request.urlopen", return_value=mock_resp):
            validator = OAuthTokenValidator(
                issuer=TEST_ISSUER,
                audience=TEST_AUDIENCE,
            )
        assert validator._jwks_uri == discovered_uri

    def test_oidc_discovery_extracts_userinfo_endpoint(self) -> None:
        """OIDC discovery extracts both jwks_uri and userinfo_endpoint."""
        from unittest.mock import patch

        discovered_uri = "https://auth.example.com/oauth2/jwks"
        userinfo_uri = "https://auth.example.com/oauth2/userinfo"
        oidc_config = json.dumps(
            {
                "jwks_uri": discovered_uri,
                "userinfo_endpoint": userinfo_uri,
            }
        ).encode()

        mock_resp = MagicMock()
        mock_resp.read.return_value = oidc_config
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("mcp_awareness.oauth.urllib.request.urlopen", return_value=mock_resp):
            validator = OAuthTokenValidator(
                issuer=TEST_ISSUER,
                audience=TEST_AUDIENCE,
            )
        assert validator._jwks_uri == discovered_uri
        assert validator._userinfo_endpoint == userinfo_uri

    def test_oidc_discovery_fallback_on_failure(self) -> None:
        """When OIDC discovery fails, fall back to .well-known/jwks.json."""
        from unittest.mock import patch

        with patch(
            "mcp_awareness.oauth.urllib.request.urlopen",
            side_effect=Exception("connection refused"),
        ):
            validator = OAuthTokenValidator(
                issuer=TEST_ISSUER,
                audience=TEST_AUDIENCE,
            )
        assert validator._jwks_uri == f"{TEST_ISSUER}/.well-known/jwks.json"
        assert validator._userinfo_endpoint == ""

    def test_explicit_jwks_uri_sets_empty_userinfo(self) -> None:
        """When explicit jwks_uri is set, userinfo_endpoint is empty."""
        validator = OAuthTokenValidator(
            issuer=TEST_ISSUER,
            audience=TEST_AUDIENCE,
            jwks_uri="https://custom.example.com/keys",
        )
        assert validator._userinfo_endpoint == ""


# ---------------------------------------------------------------------------
# WellKnownMiddleware tests
# ---------------------------------------------------------------------------


class TestWellKnownMiddleware:
    @pytest.mark.anyio
    async def test_serves_protected_resource_metadata(self) -> None:
        async def inner_app(scope: Any, receive: Any, send: Any) -> None:
            pass

        app = WellKnownMiddleware(inner_app, TEST_ISSUER, host="localhost", port=8420)
        scope = {
            "type": "http",
            "path": "/.well-known/oauth-protected-resource",
            "method": "GET",
            "headers": [],
        }

        sent: list[dict[str, Any]] = []

        async def noop_receive() -> dict[str, Any]:
            return {"type": "http.request", "body": b""}

        async def capture_send(msg: dict[str, Any]) -> None:
            sent.append(msg)

        await app(scope, noop_receive, capture_send)

        body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
        data = json.loads(body)
        assert data["authorization_servers"] == [TEST_ISSUER]
        assert data["token_methods"] == ["Bearer"]
        assert "/mcp" in data["resource"]

    @pytest.mark.anyio
    async def test_mount_path_included_in_resource_url(self) -> None:
        """When mount_path is set, resource URL includes it."""

        async def inner_app(scope: Any, receive: Any, send: Any) -> None:
            pass

        app = WellKnownMiddleware(
            inner_app,
            TEST_ISSUER,
            public_url="https://mcpawareness.com",
            mount_path="/secret",
        )
        scope = {
            "type": "http",
            "path": "/.well-known/oauth-protected-resource",
            "method": "GET",
            "headers": [],
        }

        sent: list[dict[str, Any]] = []

        async def noop_receive() -> dict[str, Any]:
            return {"type": "http.request", "body": b""}

        async def capture_send(msg: dict[str, Any]) -> None:
            sent.append(msg)

        await app(scope, noop_receive, capture_send)

        body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
        data = json.loads(body)
        assert data["resource"] == "https://mcpawareness.com/secret/mcp"

    @pytest.mark.anyio
    async def test_mount_path_in_host_fallback(self) -> None:
        """Mount path is included in resource URL derived from Host header."""

        async def inner_app(scope: Any, receive: Any, send: Any) -> None:
            pass

        app = WellKnownMiddleware(inner_app, TEST_ISSUER, mount_path="/my-secret")
        scope = {
            "type": "http",
            "path": "/.well-known/oauth-protected-resource",
            "method": "GET",
            "headers": [(b"host", b"example.com")],
        }

        sent: list[dict[str, Any]] = []

        async def noop_receive() -> dict[str, Any]:
            return {"type": "http.request", "body": b""}

        async def capture_send(msg: dict[str, Any]) -> None:
            sent.append(msg)

        await app(scope, noop_receive, capture_send)

        body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
        data = json.loads(body)
        assert data["resource"] == "https://example.com/my-secret/mcp"

    @pytest.mark.anyio
    async def test_non_http_passthrough(self) -> None:
        """Non-HTTP scopes (websocket, lifespan) pass through to inner app."""
        called = False

        async def inner_app(scope: Any, receive: Any, send: Any) -> None:
            nonlocal called
            called = True

        app = WellKnownMiddleware(inner_app, TEST_ISSUER, host="localhost", port=8420)
        scope = {"type": "lifespan"}

        async def noop_receive() -> dict[str, Any]:
            return {"type": "lifespan.startup"}

        async def noop_send(msg: dict[str, Any]) -> None:
            pass

        await app(scope, noop_receive, noop_send)
        assert called

    @pytest.mark.anyio
    async def test_passes_through_other_paths(self) -> None:
        called = False

        async def inner_app(scope: Any, receive: Any, send: Any) -> None:
            nonlocal called
            called = True

        app = WellKnownMiddleware(inner_app, TEST_ISSUER, host="localhost", port=8420)
        scope = {"type": "http", "path": "/mcp", "method": "POST", "headers": []}

        async def noop_receive() -> dict[str, Any]:
            return {"type": "http.request", "body": b""}

        async def noop_send(msg: dict[str, Any]) -> None:
            pass

        await app(scope, noop_receive, noop_send)
        assert called

    @pytest.mark.anyio
    async def test_host_header_fallback(self) -> None:
        """Without public_url, resource URL is derived from Host header."""

        async def inner_app(scope: Any, receive: Any, send: Any) -> None:
            pass

        app = WellKnownMiddleware(inner_app, TEST_ISSUER)
        scope = {
            "type": "http",
            "path": "/.well-known/oauth-protected-resource",
            "method": "GET",
            "headers": [(b"host", b"mcpawareness.com")],
        }

        sent: list[dict[str, Any]] = []

        async def noop_receive() -> dict[str, Any]:
            return {"type": "http.request", "body": b""}

        async def capture_send(msg: dict[str, Any]) -> None:
            sent.append(msg)

        await app(scope, noop_receive, capture_send)

        body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
        data = json.loads(body)
        assert data["resource"] == "https://mcpawareness.com/mcp"

    @pytest.mark.anyio
    async def test_public_url_used_in_metadata(self) -> None:
        """When public_url is set, resource URL uses it instead of host:port."""

        async def inner_app(scope: Any, receive: Any, send: Any) -> None:
            pass

        app = WellKnownMiddleware(inner_app, TEST_ISSUER, public_url="https://mcpawareness.com")
        scope = {
            "type": "http",
            "path": "/.well-known/oauth-protected-resource",
            "method": "GET",
            "headers": [],
        }

        sent: list[dict[str, Any]] = []

        async def noop_receive() -> dict[str, Any]:
            return {"type": "http.request", "body": b""}

        async def capture_send(msg: dict[str, Any]) -> None:
            sent.append(msg)

        await app(scope, noop_receive, capture_send)

        body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
        data = json.loads(body)
        assert data["resource"] == "https://mcpawareness.com/mcp"


# ---------------------------------------------------------------------------
# Per-owner concurrency limit tests
# ---------------------------------------------------------------------------


class TestConcurrencyLimit:
    @pytest.mark.anyio
    async def test_429_when_slots_exhausted(self) -> None:
        """Returns 429 when all per-owner concurrency slots are taken."""
        import asyncio as _asyncio

        secret = "test-secret-at-least-32-chars-long!"
        token = jwt.encode({"sub": "busy-user"}, secret, algorithm="HS256")

        barrier = _asyncio.Event()

        async def slow_app(scope: Any, receive: Any, send: Any) -> None:
            await barrier.wait()  # Block until released

        app = AuthMiddleware(slow_app, jwt_secret=secret, max_concurrent_per_owner=1)

        scope = {
            "type": "http",
            "path": "/mcp",
            "method": "POST",
            "headers": [(b"authorization", f"Bearer {token}".encode())],
        }

        async def noop_receive() -> dict[str, Any]:
            return {"type": "http.request", "body": b""}

        # First request — takes the only slot
        first_done = _asyncio.Event()

        async def first_request() -> None:
            async def noop_send(msg: dict[str, Any]) -> None:
                pass

            await app(scope, noop_receive, noop_send)
            first_done.set()

        task = _asyncio.create_task(first_request())
        await _asyncio.sleep(0.05)  # Let first request acquire semaphore

        # Second request — should get 429
        sent: list[dict[str, Any]] = []

        async def capture_send(msg: dict[str, Any]) -> None:
            sent.append(msg)

        await app(scope, noop_receive, capture_send)

        response_start = next(m for m in sent if m["type"] == "http.response.start")
        assert response_start["status"] == 429

        body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
        assert b"Too many concurrent requests" in body

        # Release the first request
        barrier.set()
        await task


# ---------------------------------------------------------------------------
# AuthMiddleware dual auth tests
# ---------------------------------------------------------------------------


class TestDualAuth:
    @pytest.mark.anyio
    async def test_self_signed_jwt_still_works(self) -> None:
        """Existing self-signed JWT auth continues to work with OAuth configured."""
        secret = "test-secret-at-least-32-chars-long!"
        token = jwt.encode({"sub": "alice"}, secret, algorithm="HS256")

        called_with_owner: list[str] = []

        async def inner_app(scope: Any, receive: Any, send: Any) -> None:
            from mcp_awareness.server import _owner_ctx

            called_with_owner.append(_owner_ctx.get())

        mock_oauth = MagicMock()
        app = AuthMiddleware(inner_app, jwt_secret=secret, oauth_validator=mock_oauth)

        scope = {
            "type": "http",
            "path": "/mcp",
            "method": "POST",
            "headers": [(b"authorization", f"Bearer {token}".encode())],
        }

        async def noop_receive() -> dict[str, Any]:
            return {"type": "http.request", "body": b""}

        async def noop_send(msg: dict[str, Any]) -> None:
            pass

        await app(scope, noop_receive, noop_send)
        assert called_with_owner == ["alice"]
        # OAuth validator should NOT be called when self-signed JWT succeeds
        mock_oauth.validate.assert_not_called()

    @pytest.mark.anyio
    async def test_oauth_fallback_when_self_signed_fails(self) -> None:
        """When self-signed JWT fails, falls back to OAuth validation."""
        oauth_token = "oauth-token-not-valid-as-self-signed"

        mock_oauth = MagicMock()
        mock_oauth.validate.return_value = {
            "owner_id": "oauth-user",
            "email": "oauth@example.com",
        }

        called_with_owner: list[str] = []

        async def inner_app(scope: Any, receive: Any, send: Any) -> None:
            from mcp_awareness.server import _owner_ctx

            called_with_owner.append(_owner_ctx.get())

        app = AuthMiddleware(
            inner_app,
            jwt_secret="some-secret-at-least-32-chars!!!",
            oauth_validator=mock_oauth,
            auto_provision=False,
        )

        scope = {
            "type": "http",
            "path": "/mcp",
            "method": "POST",
            "headers": [(b"authorization", f"Bearer {oauth_token}".encode())],
        }

        async def noop_receive() -> dict[str, Any]:
            return {"type": "http.request", "body": b""}

        async def noop_send(msg: dict[str, Any]) -> None:
            pass

        await app(scope, noop_receive, noop_send)
        assert called_with_owner == ["oauth-user"]
        mock_oauth.validate.assert_called_once_with(oauth_token)

    @pytest.mark.anyio
    async def test_well_known_bypasses_auth(self) -> None:
        """/.well-known/ paths should not require authentication."""
        sent: list[dict[str, Any]] = []

        async def inner_app(scope: Any, receive: Any, send: Any) -> None:
            pass

        app = AuthMiddleware(inner_app, jwt_secret="secret-at-least-32-chars!!!!!!")

        scope = {
            "type": "http",
            "path": "/.well-known/oauth-protected-resource",
            "method": "GET",
            "headers": [],  # No auth header
        }

        async def noop_receive() -> dict[str, Any]:
            return {"type": "http.request", "body": b""}

        async def capture_send(msg: dict[str, Any]) -> None:
            sent.append(msg)

        await app(scope, noop_receive, capture_send)
        # Should NOT return 401 — well-known paths are public
        status_codes = [m.get("status") for m in sent if m["type"] == "http.response.start"]
        assert 401 not in status_codes

    @pytest.mark.anyio
    async def test_resolve_user_called_on_oauth(self) -> None:
        """When OAuth succeeds, _resolve_user is called with claims."""
        mock_oauth = MagicMock()
        mock_oauth.validate.return_value = {
            "owner_id": "new-user",
            "email": "new@example.com",
            "name": "New User",
            "oauth_subject": "sub-123",
            "oauth_issuer": TEST_ISSUER,
        }

        called_with_owner: list[str] = []

        async def inner_app(scope: Any, receive: Any, send: Any) -> None:
            from mcp_awareness.server import _owner_ctx

            called_with_owner.append(_owner_ctx.get())

        app = AuthMiddleware(
            inner_app,
            jwt_secret="",
            oauth_validator=mock_oauth,
            auto_provision=False,
        )

        scope = {
            "type": "http",
            "path": "/mcp",
            "method": "POST",
            "headers": [(b"authorization", b"Bearer oauth-token")],
        }

        async def noop_receive() -> dict[str, Any]:
            return {"type": "http.request", "body": b""}

        async def noop_send(msg: dict[str, Any]) -> None:
            pass

        await app(scope, noop_receive, noop_send)
        assert called_with_owner == ["new-user"]

    @pytest.mark.anyio
    async def test_bearer_case_insensitive(self) -> None:
        """RFC 7235: 'bearer' scheme is case-insensitive."""
        secret = "test-secret-at-least-32-chars-long!"
        token = jwt.encode({"sub": "alice"}, secret, algorithm="HS256")

        called_with_owner: list[str] = []

        async def inner_app(scope: Any, receive: Any, send: Any) -> None:
            from mcp_awareness.server import _owner_ctx

            called_with_owner.append(_owner_ctx.get())

        app = AuthMiddleware(inner_app, jwt_secret=secret)

        scope = {
            "type": "http",
            "path": "/mcp",
            "method": "POST",
            "headers": [(b"authorization", f"bearer {token}".encode())],
        }

        async def noop_receive() -> dict[str, Any]:
            return {"type": "http.request", "body": b""}

        async def noop_send(msg: dict[str, Any]) -> None:
            pass

        await app(scope, noop_receive, noop_send)
        assert called_with_owner == ["alice"]

    @pytest.mark.anyio
    async def test_resolve_user_without_oauth_identity(self) -> None:
        """OAuth claims missing oauth_subject/oauth_issuer skip lookup and link steps."""
        mock_oauth = MagicMock()
        mock_oauth.validate.return_value = {
            "owner_id": "minimal-user",
            "email": "min@example.com",
        }

        called_with_owner: list[str] = []

        async def inner_app(scope: Any, receive: Any, send: Any) -> None:
            from mcp_awareness.server import _owner_ctx

            called_with_owner.append(_owner_ctx.get())

        app = AuthMiddleware(
            inner_app,
            jwt_secret="",
            oauth_validator=mock_oauth,
            auto_provision=False,
        )

        scope = {
            "type": "http",
            "path": "/mcp",
            "method": "POST",
            "headers": [(b"authorization", b"Bearer oauth-token")],
        }

        async def noop_receive() -> dict[str, Any]:
            return {"type": "http.request", "body": b""}

        async def noop_send(msg: dict[str, Any]) -> None:
            pass

        await app(scope, noop_receive, noop_send)
        # Falls back to raw owner_id from claims
        assert called_with_owner == ["minimal-user"]

    @pytest.mark.anyio
    async def test_try_oauth_logs_validation_failure(self, caplog: Any) -> None:
        """OAuth validation exceptions are logged at WARNING level."""
        import logging

        mock_oauth = MagicMock()
        mock_oauth.validate.side_effect = RuntimeError("JWKS fetch failed")

        sent: list[dict[str, Any]] = []

        async def inner_app(scope: Any, receive: Any, send: Any) -> None:
            pass

        app = AuthMiddleware(inner_app, jwt_secret="", oauth_validator=mock_oauth)

        scope = {
            "type": "http",
            "path": "/mcp",
            "method": "POST",
            "headers": [(b"authorization", b"Bearer bad-token")],
        }

        async def noop_receive() -> dict[str, Any]:
            return {"type": "http.request", "body": b""}

        async def capture_send(msg: dict[str, Any]) -> None:
            sent.append(msg)

        with caplog.at_level(logging.WARNING, logger="mcp_awareness.middleware"):
            await app(scope, noop_receive, capture_send)

        assert any("OAuth token validation failed" in r.message for r in caplog.records)
        # Should still return 401
        response_start = next(m for m in sent if m["type"] == "http.response.start")
        assert response_start["status"] == 401

    @pytest.mark.anyio
    async def test_resolve_user_logs_failure(self, monkeypatch: Any, caplog: Any) -> None:
        """User resolution exceptions are logged at WARNING level."""
        import logging

        import mcp_awareness.server as server_mod

        broken_store = MagicMock()
        broken_store.get_user_by_oauth.side_effect = RuntimeError("db down")
        monkeypatch.setattr(server_mod, "store", broken_store)

        mock_oauth = MagicMock()
        mock_oauth.validate.return_value = {
            "owner_id": "log-test-user",
            "oauth_subject": "sub",
            "oauth_issuer": TEST_ISSUER,
        }

        called_with_owner: list[str] = []

        async def inner_app(scope: Any, receive: Any, send: Any) -> None:
            from mcp_awareness.server import _owner_ctx

            called_with_owner.append(_owner_ctx.get())

        app = AuthMiddleware(
            inner_app, jwt_secret="", oauth_validator=mock_oauth, auto_provision=False
        )
        scope = {
            "type": "http",
            "path": "/mcp",
            "method": "POST",
            "headers": [(b"authorization", b"Bearer token")],
        }

        async def noop_receive() -> dict[str, Any]:
            return {"type": "http.request", "body": b""}

        async def noop_send(msg: dict[str, Any]) -> None:
            pass

        with caplog.at_level(logging.WARNING, logger="mcp_awareness.middleware"):
            await app(scope, noop_receive, noop_send)

        assert any("User resolution failed" in r.message for r in caplog.records)
        # Request should still succeed with raw owner_id
        assert called_with_owner == ["log-test-user"]

    @pytest.mark.anyio
    async def test_401_includes_www_authenticate(self) -> None:
        """401 responses include WWW-Authenticate with resource_metadata URL."""
        sent: list[dict[str, Any]] = []

        async def inner_app(scope: Any, receive: Any, send: Any) -> None:
            pass

        mock_oauth = MagicMock()
        mock_oauth.validate.side_effect = Exception("fail")
        app = AuthMiddleware(
            inner_app,
            jwt_secret="",
            oauth_validator=mock_oauth,
            resource_metadata_url="/.well-known/oauth-protected-resource",
        )

        scope = {
            "type": "http",
            "path": "/mcp",
            "method": "POST",
            "headers": [(b"authorization", b"Bearer bad-token")],
        }

        async def noop_receive() -> dict[str, Any]:
            return {"type": "http.request", "body": b""}

        async def capture_send(msg: dict[str, Any]) -> None:
            sent.append(msg)

        await app(scope, noop_receive, capture_send)

        response_start = next(m for m in sent if m["type"] == "http.response.start")
        assert response_start["status"] == 401
        headers = dict(response_start.get("headers", []))
        www_auth = headers.get(b"www-authenticate", b"").decode()
        assert "resource_metadata" in www_auth


# ---------------------------------------------------------------------------
# Auto-provisioning tests
# ---------------------------------------------------------------------------


class TestAutoProvisionIntegration:
    """Integration test: middleware auto-provision with a real store."""

    @pytest.fixture(autouse=True)
    def _cleanup_integration_users(self, store: Any) -> Any:
        yield
        with store._pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
            cur.execute(
                "DELETE FROM users WHERE id IN "
                "('integration-user', 'linked-alice', 'cli-bob', 'failing-user')"
            )

    @pytest.mark.anyio
    async def test_ensure_user_creates_record(self, store: Any, monkeypatch: Any) -> None:
        """_ensure_user calls store.create_user_if_not_exists through the server module."""
        import mcp_awareness.server as server_mod

        # Point the middleware at our test store
        monkeypatch.setattr(server_mod, "store", store)

        async def inner_app(scope: Any, receive: Any, send: Any) -> None:
            pass

        mock_oauth = MagicMock()
        mock_oauth.validate.return_value = {
            "owner_id": "integration-user",
            "email": "int@example.com",
            "name": "Integration",
            "oauth_subject": "int-sub",
            "oauth_issuer": TEST_ISSUER,
        }

        app = AuthMiddleware(
            inner_app,
            jwt_secret="",
            oauth_validator=mock_oauth,
            auto_provision=True,
        )

        scope = {
            "type": "http",
            "path": "/mcp",
            "method": "POST",
            "headers": [(b"authorization", b"Bearer oauth-token")],
        }

        async def noop_receive() -> dict[str, Any]:
            return {"type": "http.request", "body": b""}

        async def noop_send(msg: dict[str, Any]) -> None:
            pass

        await app(scope, noop_receive, noop_send)

        # Verify user was created in the real store
        user = store.get_user("integration-user")
        assert user is not None
        assert user["email"] == "int@example.com"

    @pytest.mark.anyio
    async def test_resolve_finds_already_linked_user(self, store: Any, monkeypatch: Any) -> None:
        """OAuth login resolves to existing user via oauth_subject lookup."""
        import mcp_awareness.server as server_mod

        monkeypatch.setattr(server_mod, "store", store)

        # Pre-create a linked user
        store.create_user_if_not_exists(
            "linked-alice", "alice@example.com", "Alice", "alice-sub", TEST_ISSUER
        )

        called_with_owner: list[str] = []

        async def inner_app(scope: Any, receive: Any, send: Any) -> None:
            from mcp_awareness.server import _owner_ctx

            called_with_owner.append(_owner_ctx.get())

        mock_oauth = MagicMock()
        mock_oauth.validate.return_value = {
            "owner_id": "alice-sub",
            "email": "alice@example.com",
            "oauth_subject": "alice-sub",
            "oauth_issuer": TEST_ISSUER,
        }

        app = AuthMiddleware(
            inner_app, jwt_secret="", oauth_validator=mock_oauth, auto_provision=False
        )
        scope = {
            "type": "http",
            "path": "/mcp",
            "method": "POST",
            "headers": [(b"authorization", b"Bearer oauth-token")],
        }

        async def noop_receive() -> dict[str, Any]:
            return {"type": "http.request", "body": b""}

        async def noop_send(msg: dict[str, Any]) -> None:
            pass

        await app(scope, noop_receive, noop_send)
        # Should resolve to the existing user's ID, not the raw sub claim
        assert called_with_owner == ["linked-alice"]

    @pytest.mark.anyio
    async def test_resolve_links_pre_provisioned_user_by_email(
        self, store: Any, monkeypatch: Any
    ) -> None:
        """First OAuth login links to a pre-provisioned user matched by email."""
        import mcp_awareness.server as server_mod

        monkeypatch.setattr(server_mod, "store", store)

        # Pre-provision via CLI (no OAuth identity)
        store.create_user_if_not_exists("cli-bob", "bob@example.com", "Bob")

        called_with_owner: list[str] = []

        async def inner_app(scope: Any, receive: Any, send: Any) -> None:
            from mcp_awareness.server import _owner_ctx

            called_with_owner.append(_owner_ctx.get())

        mock_oauth = MagicMock()
        mock_oauth.validate.return_value = {
            "owner_id": "bob-sub-xyz",
            "email": "bob@example.com",
            "oauth_subject": "bob-sub-xyz",
            "oauth_issuer": TEST_ISSUER,
        }

        app = AuthMiddleware(
            inner_app, jwt_secret="", oauth_validator=mock_oauth, auto_provision=False
        )
        scope = {
            "type": "http",
            "path": "/mcp",
            "method": "POST",
            "headers": [(b"authorization", b"Bearer oauth-token")],
        }

        async def noop_receive() -> dict[str, Any]:
            return {"type": "http.request", "body": b""}

        async def noop_send(msg: dict[str, Any]) -> None:
            pass

        await app(scope, noop_receive, noop_send)
        # Should resolve to the pre-provisioned user's ID via email linking
        assert called_with_owner == ["cli-bob"]
        # Verify OAuth identity was linked
        user = store.get_user_by_oauth(TEST_ISSUER, "bob-sub-xyz")
        assert user is not None
        assert user["id"] == "cli-bob"

    @pytest.mark.anyio
    async def test_resolve_enriches_missing_profile_fields(
        self, store: Any, monkeypatch: Any
    ) -> None:
        """Returning user with missing email/name gets enriched from token claims."""
        import mcp_awareness.server as server_mod

        monkeypatch.setattr(server_mod, "store", store)

        # Create user without email or display_name (simulates auto-provision
        # from a token that lacked those claims)
        store.create_user_if_not_exists("linked-alice", None, None, "alice-sub", TEST_ISSUER)
        user = store.get_user("linked-alice")
        assert user is not None
        assert user["email"] is None
        assert user["display_name"] is None

        called_with_owner: list[str] = []

        async def inner_app(scope: Any, receive: Any, send: Any) -> None:
            from mcp_awareness.server import _owner_ctx

            called_with_owner.append(_owner_ctx.get())

        # Second login — this time the token has email and name
        mock_oauth = MagicMock()
        mock_oauth.validate.return_value = {
            "owner_id": "alice-sub",
            "email": "alice@example.com",
            "name": "Alice",
            "oauth_subject": "alice-sub",
            "oauth_issuer": TEST_ISSUER,
        }

        app = AuthMiddleware(
            inner_app, jwt_secret="", oauth_validator=mock_oauth, auto_provision=False
        )
        scope = {
            "type": "http",
            "path": "/mcp",
            "method": "POST",
            "headers": [(b"authorization", b"Bearer oauth-token")],
        }

        async def noop_receive() -> dict[str, Any]:
            return {"type": "http.request", "body": b""}

        async def noop_send(msg: dict[str, Any]) -> None:
            pass

        await app(scope, noop_receive, noop_send)
        assert called_with_owner == ["linked-alice"]

        # Verify profile was enriched
        user = store.get_user("linked-alice")
        assert user is not None
        assert user["email"] == "alice@example.com"
        assert user["display_name"] == "Alice"


class TestAutoProvisionFailure:
    """Verify _ensure_user swallows exceptions gracefully."""

    @pytest.mark.anyio
    async def test_ensure_user_exception_swallowed(self, monkeypatch: Any) -> None:
        """Auto-provisioning failure must not block the request."""
        import mcp_awareness.server as server_mod

        broken_store = MagicMock()
        broken_store.get_user_by_oauth.return_value = None
        broken_store.link_oauth_identity.return_value = None
        broken_store.create_user_if_not_exists.side_effect = RuntimeError("db down")
        monkeypatch.setattr(server_mod, "store", broken_store)

        mock_oauth = MagicMock()
        mock_oauth.validate.return_value = {
            "owner_id": "failing-user",
            "oauth_subject": "sub",
            "oauth_issuer": TEST_ISSUER,
        }

        called_with_owner: list[str] = []

        async def inner_app(scope: Any, receive: Any, send: Any) -> None:
            from mcp_awareness.server import _owner_ctx

            called_with_owner.append(_owner_ctx.get())

        app = AuthMiddleware(
            inner_app, jwt_secret="", oauth_validator=mock_oauth, auto_provision=True
        )
        scope = {
            "type": "http",
            "path": "/mcp",
            "method": "POST",
            "headers": [(b"authorization", b"Bearer token")],
        }

        async def noop_receive() -> dict[str, Any]:
            return {"type": "http.request", "body": b""}

        async def noop_send(msg: dict[str, Any]) -> None:
            pass

        await app(scope, noop_receive, noop_send)
        # Request should succeed despite provisioning failure
        assert called_with_owner == ["failing-user"]


class TestServerWiring:
    def test_build_oauth_validator_returns_none_without_issuer(self) -> None:
        """No OAuth validator when OAUTH_ISSUER is empty."""
        from mcp_awareness import server as server_mod

        original = server_mod.OAUTH_ISSUER
        try:
            server_mod.OAUTH_ISSUER = ""
            assert server_mod._build_oauth_validator() is None
        finally:
            server_mod.OAUTH_ISSUER = original

    def test_wrap_with_auth_uses_oauth_when_issuer_set(self) -> None:
        """_wrap_with_auth creates AuthMiddleware with OAuth validator when issuer is set."""
        from mcp_awareness import server as server_mod

        orig_issuer = server_mod.OAUTH_ISSUER
        orig_required = server_mod.AUTH_REQUIRED
        orig_secret = server_mod.JWT_SECRET
        try:
            server_mod.OAUTH_ISSUER = TEST_ISSUER
            server_mod.AUTH_REQUIRED = True
            server_mod.JWT_SECRET = ""  # No self-signed — OAuth only

            async def dummy(scope: Any, receive: Any, send: Any) -> None:
                pass

            wrapped = server_mod._wrap_with_auth(dummy)
            assert isinstance(wrapped, AuthMiddleware)
            assert wrapped.oauth_validator is not None
        finally:
            server_mod.OAUTH_ISSUER = orig_issuer
            server_mod.AUTH_REQUIRED = orig_required
            server_mod.JWT_SECRET = orig_secret

    def test_build_oauth_validator_returns_validator_with_issuer(self) -> None:
        """OAuth validator created when OAUTH_ISSUER is set."""
        from mcp_awareness import server as server_mod

        original = server_mod.OAUTH_ISSUER
        try:
            server_mod.OAUTH_ISSUER = TEST_ISSUER
            validator = server_mod._build_oauth_validator()
            assert validator is not None
            from mcp_awareness.oauth import OAuthTokenValidator

            assert isinstance(validator, OAuthTokenValidator)
        finally:
            server_mod.OAUTH_ISSUER = original


class TestAutoProvisioning:
    @pytest.fixture(autouse=True)
    def _cleanup_oauth_users(self, store: Any) -> Any:
        yield
        # Clean up OAuth test users that aren't covered by conftest clear(TEST_OWNER)
        with store._pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
            cur.execute(
                "DELETE FROM users WHERE id LIKE 'oauth-%' OR id LIKE 'pre-%' OR id LIKE 'linked-%'"
            )

    def test_create_user_with_oauth_identity(self, store: Any) -> None:
        """Auto-provisioning stores OAuth identity fields."""
        store.create_user_if_not_exists(
            "oauth-carol", "carol@example.com", "Carol", "carol-sub-123", TEST_ISSUER
        )
        user = store.get_user("oauth-carol")
        assert user is not None
        assert user["id"] == "oauth-carol"

    def test_get_user_by_oauth(self, store: Any) -> None:
        """Look up user by OAuth issuer + subject pair."""
        store.create_user_if_not_exists(
            "oauth-dan", "dan@example.com", "Dan", "dan-sub-456", TEST_ISSUER
        )
        user = store.get_user_by_oauth(TEST_ISSUER, "dan-sub-456")
        assert user is not None
        assert user["id"] == "oauth-dan"

    def test_get_user_by_oauth_not_found(self, store: Any) -> None:
        user = store.get_user_by_oauth(TEST_ISSUER, "nonexistent-sub")
        assert user is None

    def test_create_user_if_not_exists(self, store: Any) -> None:
        """Auto-provisioning creates a user that didn't exist."""
        store.create_user_if_not_exists("oauth-alice", "alice@example.com", "Alice")
        user = store.get_user("oauth-alice")
        assert user is not None
        assert user["id"] == "oauth-alice"
        assert user["email"] == "alice@example.com"
        assert user["display_name"] == "Alice"

    def test_create_user_if_not_exists_no_op_when_exists(self, store: Any) -> None:
        """Auto-provisioning is a no-op for existing users."""
        store.create_user_if_not_exists("oauth-bob", "bob@example.com", "Bob")
        # Create again with different email — should NOT update
        store.create_user_if_not_exists("oauth-bob", "new@example.com", "New Bob")
        user = store.get_user("oauth-bob")
        assert user is not None
        assert user["email"] == "bob@example.com"  # Original preserved

    def test_get_user_returns_none_for_unknown(self, store: Any) -> None:
        user = store.get_user("nonexistent-user")
        assert user is None

    def test_link_oauth_identity_by_email(self, store: Any) -> None:
        """Pre-provisioned user (CLI) gets linked on first OAuth login by email match."""
        # Pre-provision via CLI (no OAuth identity yet)
        store.create_user_if_not_exists("pre-user", "pre@example.com", "Pre User")
        # First OAuth login — link by email
        linked_id = store.link_oauth_identity("pre-sub-789", TEST_ISSUER, "pre@example.com")
        assert linked_id == "pre-user"
        # Verify OAuth columns populated
        user = store.get_user_by_oauth(TEST_ISSUER, "pre-sub-789")
        assert user is not None
        assert user["id"] == "pre-user"

    def test_link_oauth_identity_no_match(self, store: Any) -> None:
        """Linking returns None when no user has the given email."""
        linked_id = store.link_oauth_identity("orphan-sub", TEST_ISSUER, "nobody@example.com")
        assert linked_id is None

    def test_link_oauth_identity_already_linked(self, store: Any) -> None:
        """Linking is a no-op if user already has an OAuth identity."""
        store.create_user_if_not_exists(
            "linked-user", "linked@example.com", "Linked", "existing-sub", TEST_ISSUER
        )
        # Try to link again with different sub — should not overwrite
        linked_id = store.link_oauth_identity("new-sub", TEST_ISSUER, "linked@example.com")
        assert linked_id is None  # Already linked, no update

    def test_update_user_profile_fills_missing_email(self, store: Any) -> None:
        """User created without email gets email populated on enrichment."""
        store.create_user_if_not_exists("oauth-noemail", None, None, "noemail-sub", TEST_ISSUER)
        user = store.get_user("oauth-noemail")
        assert user is not None
        assert user["email"] is None

        store.update_user_profile("oauth-noemail", email="enriched@example.com")
        user = store.get_user("oauth-noemail")
        assert user is not None
        assert user["email"] == "enriched@example.com"

    def test_update_user_profile_does_not_overwrite_existing_email(self, store: Any) -> None:
        """Existing email is NOT overwritten by a different email."""
        store.create_user_if_not_exists(
            "oauth-hasemail", "original@example.com", None, "hasemail-sub", TEST_ISSUER
        )
        store.update_user_profile("oauth-hasemail", email="different@example.com")
        user = store.get_user("oauth-hasemail")
        assert user is not None
        assert user["email"] == "original@example.com"  # Original preserved

    def test_update_user_profile_fills_missing_display_name(self, store: Any) -> None:
        """User created without display_name gets it populated on enrichment."""
        store.create_user_if_not_exists(
            "oauth-noname", "noname@example.com", None, "noname-sub", TEST_ISSUER
        )
        store.update_user_profile("oauth-noname", display_name="Enriched Name")
        user = store.get_user("oauth-noname")
        assert user is not None
        assert user["display_name"] == "Enriched Name"

    def test_update_user_profile_does_not_overwrite_existing_display_name(self, store: Any) -> None:
        """Existing display_name is NOT overwritten."""
        store.create_user_if_not_exists(
            "oauth-hasname", "hasname@example.com", "Original Name", "hasname-sub", TEST_ISSUER
        )
        store.update_user_profile("oauth-hasname", display_name="New Name")
        user = store.get_user("oauth-hasname")
        assert user is not None
        assert user["display_name"] == "Original Name"  # Original preserved


# ---------------------------------------------------------------------------
# Userinfo endpoint tests
# ---------------------------------------------------------------------------


class TestFetchUserinfo:
    def _make_validator_with_userinfo(self, userinfo_endpoint: str = "") -> OAuthTokenValidator:
        """Create a validator with a pre-set userinfo endpoint (skip real discovery)."""
        from unittest.mock import patch

        with patch(
            "mcp_awareness.oauth.urllib.request.urlopen",
            side_effect=Exception("no network"),
        ):
            validator = OAuthTokenValidator(
                issuer=TEST_ISSUER,
                audience=TEST_AUDIENCE,
            )
        validator._userinfo_endpoint = userinfo_endpoint
        return validator

    def test_returns_profile_data(self) -> None:
        """fetch_userinfo returns parsed profile when endpoint is configured."""
        from unittest.mock import patch

        validator = self._make_validator_with_userinfo("https://auth.example.com/oauth2/userinfo")
        userinfo_response = json.dumps(
            {
                "sub": "user_123",
                "email": "alice@example.com",
                "name": "Alice Smith",
            }
        ).encode()

        mock_resp = MagicMock()
        mock_resp.read.return_value = userinfo_response
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("mcp_awareness.oauth.urllib.request.urlopen", return_value=mock_resp):
            result = validator.fetch_userinfo("fake-token")

        assert result["email"] == "alice@example.com"
        assert result["name"] == "Alice Smith"
        assert result["sub"] == "user_123"

    def test_returns_empty_dict_when_no_endpoint(self) -> None:
        """fetch_userinfo returns {} when no userinfo endpoint is configured."""
        validator = self._make_validator_with_userinfo("")
        result = validator.fetch_userinfo("fake-token")
        assert result == {}

    def test_returns_empty_dict_on_network_failure(self) -> None:
        """fetch_userinfo returns {} when the HTTP request fails."""
        from unittest.mock import patch

        validator = self._make_validator_with_userinfo("https://auth.example.com/oauth2/userinfo")
        with patch(
            "mcp_awareness.oauth.urllib.request.urlopen",
            side_effect=Exception("connection refused"),
        ):
            result = validator.fetch_userinfo("fake-token")
        assert result == {}

    def test_sends_bearer_token(self) -> None:
        """fetch_userinfo sends the access token in the Authorization header."""
        from unittest.mock import patch

        validator = self._make_validator_with_userinfo("https://auth.example.com/oauth2/userinfo")
        userinfo_response = json.dumps({"sub": "user_123"}).encode()

        mock_resp = MagicMock()
        mock_resp.read.return_value = userinfo_response
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch(
            "mcp_awareness.oauth.urllib.request.urlopen", return_value=mock_resp
        ) as mock_urlopen:
            validator.fetch_userinfo("my-secret-token")
            req = mock_urlopen.call_args[0][0]
            assert req.get_header("Authorization") == "Bearer my-secret-token"

    def test_filters_non_string_values(self) -> None:
        """fetch_userinfo only returns string values from the response."""
        from unittest.mock import patch

        validator = self._make_validator_with_userinfo("https://auth.example.com/oauth2/userinfo")
        # Response includes non-string fields (e.g. boolean, int)
        userinfo_response = json.dumps(
            {
                "sub": "user_123",
                "email": "alice@example.com",
                "email_verified": True,
                "updated_at": 1234567890,
            }
        ).encode()

        mock_resp = MagicMock()
        mock_resp.read.return_value = userinfo_response
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("mcp_awareness.oauth.urllib.request.urlopen", return_value=mock_resp):
            result = validator.fetch_userinfo("fake-token")

        assert result == {"sub": "user_123", "email": "alice@example.com"}
        assert "email_verified" not in result
        assert "updated_at" not in result
