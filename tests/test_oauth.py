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


# ---------------------------------------------------------------------------
# WellKnownMiddleware tests
# ---------------------------------------------------------------------------


class TestWellKnownMiddleware:
    @pytest.mark.anyio
    async def test_serves_protected_resource_metadata(self) -> None:
        async def inner_app(scope: Any, receive: Any, send: Any) -> None:
            pass

        app = WellKnownMiddleware(inner_app, TEST_ISSUER, "localhost", 8420)
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
    async def test_passes_through_other_paths(self) -> None:
        called = False

        async def inner_app(scope: Any, receive: Any, send: Any) -> None:
            nonlocal called
            called = True

        app = WellKnownMiddleware(inner_app, TEST_ISSUER, "localhost", 8420)
        scope = {"type": "http", "path": "/mcp", "method": "POST", "headers": []}

        async def noop_receive() -> dict[str, Any]:
            return {"type": "http.request", "body": b""}

        async def noop_send(msg: dict[str, Any]) -> None:
            pass

        await app(scope, noop_receive, noop_send)
        assert called


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
    async def test_auto_provision_called_on_oauth(self) -> None:
        """When auto_provision=True, _ensure_user is called with OAuth claims."""
        mock_oauth = MagicMock()
        mock_oauth.validate.return_value = {
            "owner_id": "new-user",
            "email": "new@example.com",
            "name": "New User",
            "oauth_subject": "sub-123",
            "oauth_issuer": TEST_ISSUER,
        }

        async def inner_app(scope: Any, receive: Any, send: Any) -> None:
            pass

        app = AuthMiddleware(
            inner_app,
            jwt_secret="",
            oauth_validator=mock_oauth,
            auto_provision=True,
        )

        # Patch _ensure_user to verify it's called
        ensure_calls: list[tuple[Any, ...]] = []

        def tracking_ensure(*args: Any) -> None:
            ensure_calls.append(args)

        app._ensure_user = tracking_ensure  # type: ignore[assignment]

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
        assert len(ensure_calls) == 1
        assert ensure_calls[0][0] == "new-user"
        assert ensure_calls[0][1] == "new@example.com"

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


class TestAutoProvisionFailure:
    """Verify _ensure_user swallows exceptions gracefully."""

    @pytest.mark.anyio
    async def test_ensure_user_exception_swallowed(self, monkeypatch: Any) -> None:
        """Auto-provisioning failure must not block the request."""
        import mcp_awareness.server as server_mod

        broken_store = MagicMock()
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
