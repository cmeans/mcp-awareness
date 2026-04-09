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


def _start_stateless_server(pg_dsn: str, port: int) -> str:
    """Start a server in stateless HTTP mode. Returns base URL."""
    os.environ["AWARENESS_DATABASE_URL"] = pg_dsn
    os.environ["AWARENESS_TRANSPORT"] = "streamable-http"
    os.environ["AWARENESS_HOST"] = "127.0.0.1"
    os.environ["AWARENESS_PORT"] = str(port)
    os.environ["AWARENESS_MOUNT_PATH"] = ""
    os.environ["AWARENESS_STATELESS_HTTP"] = "true"
    os.environ.pop("AWARENESS_EMBEDDING_PROVIDER", None)
    os.environ.pop("AWARENESS_SESSION_DATABASE_URL", None)

    import mcp_awareness.server as server_mod

    server_mod._LazyStore._instance = None
    importlib.reload(server_mod)

    t = threading.Thread(target=server_mod.main, daemon=True)
    t.start()

    base = f"http://127.0.0.1:{port}"
    _wait_for_health(base, timeout=10.0)
    return base


def _cleanup_stateless_env() -> None:
    """Remove stateless env var and reload to avoid leaking into other tests."""
    os.environ.pop("AWARENESS_STATELESS_HTTP", None)
    import mcp_awareness.server as server_mod

    importlib.reload(server_mod)


class TestStatelessHTTPIntegration:
    """Integration tests for stateless HTTP mode."""

    @pytest.fixture(autouse=True)
    def _cleanup_after(self) -> None:  # type: ignore[return]
        yield
        _cleanup_stateless_env()

    def test_tool_call_without_session(self, pg_dsn: str) -> None:
        """Tool calls work in stateless mode without initialize handshake."""
        port = _free_port()
        base = _start_stateless_server(pg_dsn, port)

        # Call tools/list directly — no initialize, no session ID
        r = httpx.post(
            f"{base}/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/list",
                "params": {},
            },
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
            timeout=5.0,
        )
        assert r.status_code == 200
        # No session ID in response headers
        assert "mcp-session-id" not in r.headers

    def test_no_session_id_in_responses(self, pg_dsn: str) -> None:
        """Stateless mode never returns Mcp-Session-Id header."""
        port = _free_port()
        base = _start_stateless_server(pg_dsn, port)

        # Even an initialize request should not return a session ID
        r = httpx.post(
            f"{base}/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "0.1.0"},
                },
            },
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
            timeout=5.0,
        )
        assert r.status_code == 200
        assert "mcp-session-id" not in r.headers

    def test_no_409_after_restart(self, pg_dsn: str) -> None:
        """In stateless mode, a stale session ID doesn't cause 409."""
        port = _free_port()
        base = _start_stateless_server(pg_dsn, port)

        # Send a request with a fake stale session ID
        r = httpx.post(
            f"{base}/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/list",
                "params": {},
            },
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
                "Mcp-Session-Id": "stale-session-id-from-previous-life",
            },
            timeout=5.0,
        )
        # Should NOT be 409 — stateless mode ignores session IDs
        assert r.status_code != 409

    def test_concurrent_stateless_requests(self, pg_dsn: str) -> None:
        """Multiple concurrent requests each get independent transports."""
        import concurrent.futures

        port = _free_port()
        base = _start_stateless_server(pg_dsn, port)

        def _make_request(request_id: int) -> int:
            r = httpx.post(
                f"{base}/mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": "tools/list",
                    "params": {},
                },
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json, text/event-stream",
                },
                timeout=10.0,
            )
            return r.status_code

        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
            futures = [pool.submit(_make_request, i) for i in range(5)]
            results = [f.result() for f in concurrent.futures.as_completed(futures)]

        assert all(status == 200 for status in results), f"Statuses: {results}"
