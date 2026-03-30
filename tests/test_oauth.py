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
    @pytest.mark.asyncio(loop_scope="function")
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

    @pytest.mark.asyncio(loop_scope="function")
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
    @pytest.mark.asyncio(loop_scope="function")
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

    @pytest.mark.asyncio(loop_scope="function")
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

    @pytest.mark.asyncio(loop_scope="function")
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

    @pytest.mark.asyncio(loop_scope="function")
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


class TestAutoProvisioning:
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
