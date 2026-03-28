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
