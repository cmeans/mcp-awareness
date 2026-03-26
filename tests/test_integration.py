"""Integration tests for server startup paths.

These tests start the actual server on streamable-http transport with a real
Postgres backend, then make HTTP requests to verify health, middleware routing,
and the MCP endpoint.  They cover ``_run()``, ``_create_store()``, middleware
instantiation, and transport config — the startup paths that unit tests can't
reach.
"""

from __future__ import annotations

import importlib
import os
import socket
import threading
import time

import httpx
import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _free_port() -> int:
    """Return an OS-assigned ephemeral port that is (momentarily) free."""
    with socket.socket() as s:
        s.bind(("", 0))
        return s.getsockname()[1]


def _wait_for_health(base_url: str, *, timeout: float = 5.0) -> None:
    """Poll the health endpoint until it responds or *timeout* expires."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            r = httpx.get(f"{base_url}/health", timeout=1.0)
            if r.status_code == 200:
                return
        except httpx.ConnectError:
            pass
        time.sleep(0.15)
    raise TimeoutError(f"Server at {base_url} did not become ready within {timeout}s")


def _start_server(pg_dsn: str, port: int, mount_path: str = "") -> str:
    """Configure env, reload the server module, and run it in a daemon thread.

    Returns the base URL (e.g. ``http://127.0.0.1:<port>``).
    """
    # Set env vars *before* reloading so module-level reads pick them up.
    os.environ["AWARENESS_DATABASE_URL"] = pg_dsn
    os.environ["AWARENESS_TRANSPORT"] = "streamable-http"
    os.environ["AWARENESS_HOST"] = "127.0.0.1"
    os.environ["AWARENESS_PORT"] = str(port)
    os.environ["AWARENESS_MOUNT_PATH"] = mount_path
    # Disable embeddings — no Ollama in test
    os.environ.pop("AWARENESS_EMBEDDING_PROVIDER", None)

    # Reload the module so module-level constants (TRANSPORT, HOST, PORT, …)
    # and the _LazyStore are re-evaluated from the fresh env vars.
    import mcp_awareness.server as server_mod

    # Reset the _LazyStore so the next access creates a new store with the
    # current DATABASE_URL.
    server_mod._LazyStore._instance = None

    importlib.reload(server_mod)

    def _target() -> None:
        server_mod.main()

    t = threading.Thread(target=_target, daemon=True)
    t.start()

    base = f"http://127.0.0.1:{port}"
    if mount_path:
        _wait_for_health_at(f"{base}{mount_path}", timeout=10.0)
    else:
        _wait_for_health(base, timeout=10.0)
    return base


def _wait_for_health_at(base_url: str, *, timeout: float = 5.0) -> None:
    """Poll *base_url*/health until 200 or timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            r = httpx.get(f"{base_url}/health", timeout=1.0)
            if r.status_code == 200:
                return
        except httpx.ConnectError:
            pass
        time.sleep(0.15)
    raise TimeoutError(f"Server at {base_url}/health did not respond within {timeout}s")


# ---------------------------------------------------------------------------
# Marker — all tests in this module are integration tests.
# ---------------------------------------------------------------------------

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestHealthEndpoint:
    """Start server (no secret path) and verify /health."""

    def test_health_returns_ok(self, pg_dsn: str) -> None:
        port = _free_port()
        base = _start_server(pg_dsn, port)
        _wait_for_health(base, timeout=10.0)

        r = httpx.get(f"{base}/health", timeout=3.0)
        assert r.status_code == 200

        body = r.json()
        assert body["status"] == "ok"
        assert body["transport"] == "streamable-http"
        assert "uptime_sec" in body
        assert "timestamp" in body


class TestSecretPathMiddleware:
    """Start server with AWARENESS_MOUNT_PATH and verify routing."""

    def test_secret_path_routing(self, pg_dsn: str) -> None:
        port = _free_port()
        mount = "/secret"

        # Set env and reload
        os.environ["AWARENESS_DATABASE_URL"] = pg_dsn
        os.environ["AWARENESS_TRANSPORT"] = "streamable-http"
        os.environ["AWARENESS_HOST"] = "127.0.0.1"
        os.environ["AWARENESS_PORT"] = str(port)
        os.environ["AWARENESS_MOUNT_PATH"] = mount
        os.environ.pop("AWARENESS_EMBEDDING_PROVIDER", None)

        import mcp_awareness.server as server_mod

        server_mod._LazyStore._instance = None
        importlib.reload(server_mod)

        t = threading.Thread(target=server_mod.main, daemon=True)
        t.start()

        base = f"http://127.0.0.1:{port}"
        _wait_for_health_at(f"{base}{mount}", timeout=10.0)

        # /secret/health → 200 with health JSON
        r = httpx.get(f"{base}{mount}/health", timeout=3.0)
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "ok"

        # /other → 404 (not the secret path)
        r2 = httpx.get(f"{base}/other", timeout=3.0)
        assert r2.status_code == 404

        # /secret/mcp → should be routed to the MCP app (not 404)
        # A GET to /mcp on the MCP streamable-http app returns 405 (Method Not Allowed)
        # because MCP expects POST, but the point is it's NOT a 404.
        r3 = httpx.get(f"{base}{mount}/mcp", timeout=3.0)
        assert r3.status_code != 404


class TestMcpEndpoint:
    """Verify the /mcp endpoint exists and accepts POST."""

    def test_mcp_endpoint_responds(self, pg_dsn: str) -> None:
        port = _free_port()
        base = _start_server(pg_dsn, port)
        _wait_for_health(base, timeout=10.0)

        # POST an MCP initialize request — the server should respond with
        # a valid JSON-RPC response (not 404 or 500).
        mcp_request = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "0.1.0"},
            },
        }
        r = httpx.post(
            f"{base}/mcp",
            json=mcp_request,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
            timeout=5.0,
        )
        # MCP streamable-http should return 200 with an SSE or JSON response
        assert r.status_code == 200
