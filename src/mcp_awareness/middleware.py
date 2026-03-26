"""ASGI middleware for the awareness service.

SecretPathMiddleware — rewrites /SECRET/mcp -> /mcp, serves /SECRET/health.
HealthMiddleware — serves /health, passes everything else through.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

from .helpers import _start_time


def _health_response(transport: str) -> dict[str, Any]:
    """Build the health check response payload."""
    return {
        "status": "ok",
        "uptime_sec": round(time.monotonic() - _start_time, 1),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "transport": transport,
    }


class SecretPathMiddleware:
    """Rewrite /SECRET/mcp -> /mcp, serve /SECRET/health, reject everything else."""

    def __init__(self, app: ASGIApp, prefix: str, transport: str) -> None:
        self.app = app
        self.prefix = prefix.rstrip("/")
        self.transport = transport

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] in ("http", "websocket"):
            path: str = scope.get("path", "")
            # Health endpoint — served at /SECRET/health
            if path == f"{self.prefix}/health":
                health_resp = JSONResponse(_health_response(self.transport))
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

    def __init__(self, app: ASGIApp, transport: str) -> None:
        self.app = app
        self.transport = transport

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope.get("path") == "/health":
            health_resp = JSONResponse(_health_response(self.transport))
            await health_resp(scope, receive, send)
            return
        await self.app(scope, receive, send)
