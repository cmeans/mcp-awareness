"""ASGI middleware for health checks and secret-path routing."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

# The health response builder is injected so middleware doesn't depend on server globals.
HealthBuilder = Callable[[], dict[str, Any]]


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
        if scope["type"] == "http" and scope.get("path") == "/health":
            health_resp = JSONResponse(self.health_builder())
            await health_resp(scope, receive, send)
            return
        await self.app(scope, receive, send)
