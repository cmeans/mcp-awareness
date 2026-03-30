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

"""OAuth 2.1 resource server — JWKS-based token validation for external providers."""

from __future__ import annotations

import threading
import time

import jwt
from jwt import PyJWKClient


class OAuthTokenValidator:
    """Validates OAuth access tokens (JWTs) against an external provider's JWKS.

    Provider-agnostic: works with any OIDC-compliant provider (WorkOS, Auth0,
    Cloudflare Access, Keycloak, AWS Cognito, etc.).
    """

    def __init__(
        self,
        issuer: str,
        audience: str = "",
        jwks_uri: str = "",
        user_claim: str = "sub",
        jwks_cache_ttl: int = 3600,
    ) -> None:
        self.issuer = issuer.rstrip("/")
        self.audience = audience
        self.user_claim = user_claim

        # JWKS URI: explicit override or derive from issuer
        if jwks_uri:
            self._jwks_uri = jwks_uri
        else:
            self._jwks_uri = f"{self.issuer}/.well-known/jwks.json"

        self._jwk_client = PyJWKClient(self._jwks_uri, cache_jwk_set=True)
        self._jwks_cache_ttl = jwks_cache_ttl
        self._last_jwks_fetch: float = 0.0
        self._jwks_lock = threading.Lock()

    def validate(self, token: str) -> dict[str, str]:
        """Validate an OAuth JWT and return extracted identity claims.

        Returns:
            dict with 'owner_id' and optional 'email', 'name' keys.

        Raises:
            jwt.InvalidTokenError: if token is invalid, expired, or unverifiable.
        """
        # Refresh JWKS cache if stale (lock prevents thundering herd)
        now = time.monotonic()
        if now - self._last_jwks_fetch > self._jwks_cache_ttl:
            with self._jwks_lock:
                # Double-check after acquiring lock
                if now - self._last_jwks_fetch > self._jwks_cache_ttl:
                    self._jwk_client = PyJWKClient(self._jwks_uri, cache_jwk_set=True)
                    self._last_jwks_fetch = time.monotonic()

        signing_key = self._jwk_client.get_signing_key_from_jwt(token)

        # Build kwargs for jwt.decode
        kwargs: dict[str, object] = {
            "key": signing_key.key,
            "algorithms": ["RS256", "ES256"],
            "issuer": self.issuer,
        }
        if self.audience:
            kwargs["audience"] = self.audience
        else:
            kwargs["options"] = {"verify_aud": False}

        payload = jwt.decode(token, **kwargs)  # type: ignore[arg-type]

        # Extract owner_id from configured claim
        owner_id = payload.get(self.user_claim)
        if not owner_id:
            raise jwt.InvalidTokenError(f"Token missing '{self.user_claim}' claim")

        result: dict[str, str] = {"owner_id": str(owner_id)}

        # Extract identity fields for auto-provisioning and lookup
        if "sub" in payload:
            result["oauth_subject"] = str(payload["sub"])
        if "iss" in payload:
            result["oauth_issuer"] = str(payload["iss"])
        if "email" in payload:
            result["email"] = str(payload["email"])
        if "name" in payload:
            result["name"] = str(payload["name"])

        return result
