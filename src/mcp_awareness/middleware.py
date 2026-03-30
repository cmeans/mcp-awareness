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

"""ASGI middleware for health checks, favicon, and secret-path routing."""

from __future__ import annotations

import asyncio
import pathlib
from collections.abc import Callable
from typing import Any

from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

# The health response builder is injected so middleware doesn't depend on server globals.
HealthBuilder = Callable[[], dict[str, Any]]

# Favicon bytes loaded once at import time (~15 KB).
_FAVICON_PATH = pathlib.Path(__file__).parent / "favicon.ico"
_FAVICON_BYTES: bytes | None = _FAVICON_PATH.read_bytes() if _FAVICON_PATH.exists() else None


class SecretPathMiddleware:
    """Rewrite /SECRET/mcp -> /mcp, serve /SECRET/health, reject everything else."""

    def __init__(
        self,
        app: ASGIApp,
        prefix: str,
        health_builder: HealthBuilder,
    ) -> None:
        self.app = app
        self.prefix = prefix.rstrip("/")
        self.health_builder = health_builder

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] in ("http", "websocket"):
            path: str = scope.get("path", "")
            # Favicon — served publicly (no secret path required) so external
            # services like Google's favicon crawler can fetch it.
            if path == "/favicon.ico" and _FAVICON_BYTES is not None:
                resp = Response(_FAVICON_BYTES, media_type="image/x-icon")
                await resp(scope, receive, send)
                return
            # Health endpoint — served at /SECRET/health
            if path == f"{self.prefix}/health":
                health_resp = JSONResponse(self.health_builder())
                await health_resp(scope, receive, send)
                return
            if path.startswith(self.prefix):
                scope = dict(scope)
                scope["path"] = path[len(self.prefix) :] or "/"
                await self.app(scope, receive, send)
                return
            # Not the secret path — 404
            not_found = Response("Not Found", status_code=404)
            await not_found(scope, receive, send)
            return
        await self.app(scope, receive, send)


class HealthMiddleware:
    """Serve /health, pass everything else to the MCP app."""

    def __init__(self, app: ASGIApp, health_builder: HealthBuilder) -> None:
        self.app = app
        self.health_builder = health_builder

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            path = scope.get("path", "")
            if path == "/health":
                health_resp = JSONResponse(self.health_builder())
                await health_resp(scope, receive, send)
                return
            if path == "/favicon.ico" and _FAVICON_BYTES is not None:
                resp = Response(_FAVICON_BYTES, media_type="image/x-icon")
                await resp(scope, receive, send)
                return
        await self.app(scope, receive, send)


class WellKnownMiddleware:
    """Serve /.well-known/oauth-protected-resource (RFC 9728)."""

    def __init__(
        self,
        app: ASGIApp,
        oauth_issuer: str,
        public_url: str = "",
        host: str = "localhost",
        port: int = 8420,
        mount_path: str = "",
    ) -> None:
        self.app = app
        self.oauth_issuer = oauth_issuer.rstrip("/")
        # Use explicit public URL if set, otherwise derive from host:port
        if public_url:
            self.resource_url = f"{public_url.rstrip('/')}{mount_path}/mcp"
        else:
            self.resource_url = f"https://{host}:{port}{mount_path}/mcp"

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            path = scope.get("path", "")
            if path == "/.well-known/oauth-protected-resource":
                metadata = {
                    "resource": self.resource_url,
                    "authorization_servers": [self.oauth_issuer],
                    "token_methods": ["Bearer"],
                }
                resp = JSONResponse(metadata)
                await resp(scope, receive, send)
                return
        await self.app(scope, receive, send)


class AuthMiddleware:
    """Validate JWT Bearer token and set owner context.

    Supports dual auth: self-signed JWTs (via shared secret) and OAuth provider
    tokens (via JWKS). Self-signed is tried first; if it fails and an OAuth
    validator is configured, the token is validated against the provider's keys.

    Includes per-owner concurrency limiting to prevent a single aggressive
    client from saturating the connection pool and DOSing other tenants.
    """

    def __init__(
        self,
        app: ASGIApp,
        jwt_secret: str,
        algorithm: str = "HS256",
        oauth_validator: object | None = None,
        auto_provision: bool = False,
        resource_metadata_url: str = "",
        max_concurrent_per_owner: int = 3,
    ) -> None:
        self.app = app
        self.jwt_secret = jwt_secret
        self.algorithm = algorithm
        self.oauth_validator = oauth_validator
        self.auto_provision = auto_provision
        self.resource_metadata_url = resource_metadata_url
        self._max_concurrent = max_concurrent_per_owner
        self._owner_semaphores: dict[str, asyncio.Semaphore] = {}

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        # Skip auth for health, favicon, well-known, and non-MCP paths
        if path in ("/health", "/favicon.ico") or path.startswith("/.well-known/"):
            await self.app(scope, receive, send)
            return

        # Extract Bearer token
        headers = dict(scope.get("headers", []))
        auth_header = headers.get(b"authorization", b"").decode()
        # RFC 7235: auth scheme is case-insensitive
        if not auth_header.lower().startswith("bearer "):
            resp = self._unauthorized("Missing or invalid Authorization header")
            await resp(scope, receive, send)
            return

        token = auth_header[7:]  # Strip "Bearer " (or "bearer ", etc.)

        # Try self-signed JWT first
        owner_id, error = self._try_self_signed(token)

        # Fall back to OAuth provider validation
        if owner_id is None and self.oauth_validator is not None:
            owner_id = await self._try_oauth(token)
            if owner_id is not None:
                error = None

        if owner_id is None:
            resp = self._unauthorized(error or "Invalid token")
            await resp(scope, receive, send)
            return

        # Per-owner concurrency limit — prevents one client from DOSing others
        sem = self._owner_semaphores.get(owner_id)
        if sem is None:
            sem = asyncio.Semaphore(self._max_concurrent)
            self._owner_semaphores[owner_id] = sem

        if not sem._value:  # All slots taken — reject immediately
            resp = JSONResponse({"error": "Too many concurrent requests"}, status_code=429)
            await resp(scope, receive, send)
            return

        # Set owner context for downstream handlers
        from .server import _owner_ctx

        async with sem:
            token_reset = _owner_ctx.set(owner_id)
            try:
                await self.app(scope, receive, send)
            finally:
                _owner_ctx.reset(token_reset)

    def _try_self_signed(self, token: str) -> tuple[str | None, str | None]:
        """Validate a self-signed JWT (from mcp-awareness-token CLI).

        Returns (owner_id, error_message). On success error is None.
        On failure owner_id is None and error carries the reason.
        """
        if not self.jwt_secret:
            return None, None
        try:
            import jwt

            payload = jwt.decode(token, self.jwt_secret, algorithms=[self.algorithm])
            owner_id: str | None = payload.get("sub")
            if owner_id:
                return owner_id, None
            return None, "JWT missing 'sub' claim"
        except Exception as exc:
            import jwt as jwt_mod

            if isinstance(exc, jwt_mod.ExpiredSignatureError):
                return None, "Token expired"
            if isinstance(exc, jwt_mod.InvalidTokenError):
                return None, "Invalid token"
            return None, None

    async def _try_oauth(self, token: str) -> str | None:
        """Validate an OAuth token against the external provider's JWKS."""
        import asyncio

        from .oauth import OAuthTokenValidator

        validator: OAuthTokenValidator = self.oauth_validator  # type: ignore[assignment]
        try:
            claims = await asyncio.to_thread(validator.validate, token)
        except Exception:
            return None

        owner_id = claims["owner_id"]
        oauth_subject = claims.get("oauth_subject")
        oauth_issuer = claims.get("oauth_issuer")
        email = claims.get("email")

        # Resolve user identity: OAuth lookup → email link → auto-provision
        # Run in thread to avoid blocking event loop with sync DB calls
        resolved_id = await asyncio.to_thread(
            self._resolve_user,
            owner_id,
            email,
            claims.get("name"),
            oauth_subject,
            oauth_issuer,
        )

        return resolved_id or owner_id

    def _resolve_user(
        self,
        owner_id: str,
        email: str | None,
        display_name: str | None,
        oauth_subject: str | None,
        oauth_issuer: str | None,
    ) -> str | None:
        """Resolve OAuth token to a local user, linking or creating as needed.

        Resolution order:
        1. Look up by OAuth identity (issuer + subject) — already linked user
        2. If email present, try to link to a pre-provisioned user by email
        3. If auto_provision enabled, create a new user
        4. Otherwise return None (use owner_id from token as-is)
        """
        try:
            from .server import store

            # 1. Already linked?
            if oauth_issuer and oauth_subject:
                existing = store.get_user_by_oauth(oauth_issuer, oauth_subject)
                if existing:
                    return str(existing["id"])

            # 2. Pre-provisioned user with matching email? Link on first login.
            if email and oauth_subject and oauth_issuer:
                linked_id = store.link_oauth_identity(oauth_subject, oauth_issuer, email)
                if linked_id:
                    return str(linked_id)

            # 3. Auto-provision new user
            if self.auto_provision:
                store.create_user_if_not_exists(
                    owner_id, email, display_name, oauth_subject, oauth_issuer
                )
                return owner_id

        except Exception:
            # Don't fail the request if user resolution fails
            pass

        return None

    def _unauthorized(self, message: str) -> JSONResponse:
        """Build a 401 response with proper WWW-Authenticate header."""
        headers: dict[str, str] = {}
        if self.resource_metadata_url:
            headers["WWW-Authenticate"] = f'Bearer resource_metadata="{self.resource_metadata_url}"'
        else:
            headers["WWW-Authenticate"] = "Bearer"
        return JSONResponse({"error": message}, status_code=401, headers=headers)
