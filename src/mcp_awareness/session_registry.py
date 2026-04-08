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

"""Postgres-backed MCP session registry and ASGI middleware."""

from __future__ import annotations

import json
import logging
import pathlib
import threading
import time
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
from starlette.types import ASGIApp, Message, Receive, Scope, Send

logger = logging.getLogger(__name__)

_SQL_DIR = pathlib.Path(__file__).parent / "sql"
_sql_cache: dict[str, str] = {}


def _load_sql(name: str) -> str:
    if name not in _sql_cache:
        _sql_cache[name] = (_SQL_DIR / f"{name}.sql").read_text()
    return _sql_cache[name]


class SessionStore:
    """Postgres client for the MCP session registry."""

    def __init__(
        self,
        dsn: str,
        ttl_seconds: int = 1800,
        min_pool: int = 1,
        max_pool: int = 5,
        redirect_grace_seconds: int = 300,
    ) -> None:
        self.dsn = dsn
        self.ttl_seconds = ttl_seconds
        self.redirect_grace_seconds = redirect_grace_seconds
        self._pool: ConnectionPool[psycopg.Connection[dict[str, Any]]] = ConnectionPool(
            dsn,
            min_size=min_pool,
            max_size=max_pool,
            kwargs={"row_factory": dict_row},
            open=True,
        )
        self._last_cleanup: float = 0.0
        self._cleanup_interval: float = 60.0
        self._cleanup_thread: threading.Thread | None = None
        self.ensure_schema()

    def ensure_schema(self) -> None:
        """Create tables if they don't exist (idempotent)."""
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(_load_sql("session_create_tables"))

    def register(
        self,
        session_id: str,
        owner_id: str,
        node: str | None = None,
        protocol_version: str | None = None,
        capabilities: dict[str, Any] | None = None,
        client_info: dict[str, Any] | None = None,
    ) -> None:
        """Register a new session in the registry."""
        with self._pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
            cur.execute(
                _load_sql("session_register"),
                (
                    session_id,
                    owner_id,
                    node,
                    protocol_version,
                    json.dumps(capabilities or {}),
                    json.dumps(client_info or {}),
                    self.ttl_seconds,
                ),
            )

    def lookup(self, session_id: str) -> dict[str, Any] | None:
        """Look up a session by ID. Returns None if not found or expired."""
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(_load_sql("session_lookup"), (session_id,))
            return cur.fetchone()

    def touch(self, session_id: str) -> None:
        """Update last_seen and extend expires_at (sliding window)."""
        with self._pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
            cur.execute(_load_sql("session_touch"), (self.ttl_seconds, session_id))

    def invalidate(self, session_id: str) -> None:
        """Mark a session as expired (immediate)."""
        with self._pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
            cur.execute(_load_sql("session_invalidate"), (session_id,))

    def count_active(self, owner_id: str) -> int:
        """Count non-expired sessions for an owner."""
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(_load_sql("session_count_active"), (owner_id,))
            row = cur.fetchone()
            return row["cnt"] if row else 0

    def add_redirect(self, old_session_id: str, new_session_id: str) -> None:
        """Store a redirect mapping from old to new session_id."""
        with self._pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
            cur.execute(
                _load_sql("session_add_redirect"),
                (old_session_id, new_session_id, self.redirect_grace_seconds),
            )

    def redirect_lookup(self, session_id: str) -> str | None:
        """Look up a redirect for an old session_id. Returns new_session_id or None."""
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(_load_sql("session_redirect_lookup"), (session_id,))
            row = cur.fetchone()
            return row["new_session_id"] if row else None

    def cleanup_expired(self) -> int:
        """Purge expired sessions and redirects. Returns total rows deleted."""
        total = 0
        with self._pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
            cur.execute(_load_sql("session_cleanup_redirects"))
            total += cur.rowcount
            cur.execute(_load_sql("session_cleanup_sessions"))
            total += cur.rowcount
        return total

    def delete_redirects_to(self, session_id: str) -> None:
        """Delete all redirect mappings pointing to the given session_id."""
        with self._pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
            cur.execute(
                "DELETE FROM session_redirects WHERE new_session_id = %s",
                (session_id,),
            )

    def _schedule_cleanup(self) -> None:
        """Schedule cleanup on a background thread (debounced)."""
        now = time.monotonic()
        if now - self._last_cleanup < self._cleanup_interval:
            return
        if self._cleanup_thread is not None and self._cleanup_thread.is_alive():
            return
        self._last_cleanup = now
        self._cleanup_thread = threading.Thread(
            target=self._do_cleanup, name="session-cleanup", daemon=True
        )
        self._cleanup_thread.start()

    def _do_cleanup(self) -> None:
        """Run cleanup in a background thread."""
        try:
            self.cleanup_expired()
        except Exception as exc:
            logger.error("Session cleanup failed: %s: %s", type(exc).__name__, exc)

    def close(self) -> None:
        """Close the connection pool."""
        self._pool.close()


class SessionRegistryMiddleware:
    """ASGI middleware for MCP session persistence."""

    def __init__(
        self,
        app: ASGIApp,
        session_store: SessionStore,
        node_name: str = "unknown",
        max_sessions_per_owner: int = 10,
        mcp_path: str = "/mcp",
    ) -> None:
        self.app = app
        self.store = session_store
        self.node_name = node_name
        self.max_sessions_per_owner = max_sessions_per_owner
        self.mcp_path = mcp_path
        self._touch_debounce: dict[str, float] = {}
        self._touch_debounce_seconds = 30.0

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        method = scope.get("method", "")

        if path != self.mcp_path or method not in ("POST", "DELETE"):
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers", []))
        session_id = headers.get(b"mcp-session-id", b"").decode() or None

        if method == "POST" and session_id is None:
            await self._handle_initialize(scope, receive, send)
        elif method == "POST" and session_id is not None:
            await self._handle_subsequent(scope, receive, send, session_id)
        elif method == "DELETE":
            await self._handle_terminate(scope, receive, send, session_id)
        else:
            await self.app(scope, receive, send)

    async def _handle_initialize(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Handle initialize: pass through, capture session_id, register."""
        # Check session limit
        owner_id = self._get_owner_id()
        try:
            count = self.store.count_active(owner_id)
            if count >= self.max_sessions_per_owner:
                logger.warning(
                    "Owner %s at session limit (%d/%d)",
                    owner_id,
                    count,
                    self.max_sessions_per_owner,
                )
                await self._send_error(
                    send,
                    429,
                    f"Session limit reached ({count}/{self.max_sessions_per_owner}). "
                    "Contact admin if this is unexpected.",
                )
                return
        except Exception:
            logger.error("Session limit check failed", exc_info=True)
            # Graceful degradation: allow through

        body, replay_receive = await self._buffer_body(receive)

        # Parse JSON-RPC to extract initialize params
        capabilities: dict[str, Any] = {}
        client_info: dict[str, Any] = {}
        protocol_version: str = ""
        try:
            rpc = json.loads(body)
            params = rpc.get("params", {})
            capabilities = params.get("capabilities", {})
            client_info = params.get("clientInfo", {})
            protocol_version = params.get("protocolVersion", "")
        except (ValueError, AttributeError):
            pass

        # Capture response headers
        captured_status = 0
        captured_headers: list[tuple[bytes, bytes]] = []

        async def capturing_send(message: Message) -> None:
            nonlocal captured_status, captured_headers
            if message["type"] == "http.response.start":
                captured_status = message["status"]
                captured_headers = list(message.get("headers", []))
            await send(message)

        await self.app(scope, replay_receive, capturing_send)

        if 200 <= captured_status < 300:
            new_session_id = self._extract_session_id(captured_headers)
            if new_session_id:
                owner_id = self._get_owner_id()
                try:
                    self.store.register(
                        session_id=new_session_id,
                        owner_id=owner_id,
                        node=self.node_name,
                        protocol_version=protocol_version,
                        capabilities=capabilities,
                        client_info=client_info,
                    )
                    logger.info(
                        "Session registered: %s (owner=%s, node=%s)",
                        new_session_id,
                        owner_id,
                        self.node_name,
                    )
                    self.store._schedule_cleanup()
                except Exception:
                    logger.error("Failed to register session %s", new_session_id, exc_info=True)

    async def _handle_subsequent(
        self, scope: Scope, receive: Receive, send: Send, session_id: str
    ) -> None:
        """Handle subsequent request: lookup, validate owner, pass through or re-init."""
        # Lookup session in registry first
        session = None
        try:
            session = self.store.lookup(session_id)
        except Exception:
            logger.error("Session lookup failed for %s", session_id, exc_info=True)
            await self.app(scope, receive, send)
            return

        # If not found, check redirect table
        if session is None:
            redirect_target = None
            try:
                redirect_target = self.store.redirect_lookup(session_id)
            except Exception:
                logger.error("Redirect lookup failed for %s", session_id, exc_info=True)

            if redirect_target:
                session_id = redirect_target
                scope = self._rewrite_session_header(scope, session_id)
                # Look up the target session
                try:
                    session = self.store.lookup(session_id)
                except Exception:
                    logger.error("Session lookup failed for %s", session_id, exc_info=True)
                    await self.app(scope, receive, send)
                    return

        if session is None:
            # Not in registry — pass through (FastMCP will return its own error)
            await self.app(scope, receive, send)
            return

        # Validate owner
        owner_id = self._get_owner_id()
        if session["owner_id"] != owner_id:
            logger.warning(
                "Owner mismatch: session %s owned by %s, request from %s",
                session_id,
                session["owner_id"],
                owner_id,
            )
            await self._send_error(send, 403, "Forbidden: session owner mismatch")
            return

        # Buffer body for potential re-init replay
        body, replay_receive = await self._buffer_body(receive)

        # Pass through to FastMCP and capture response (don't send to client yet)
        captured_status = 0
        captured_response_parts: list[Message] = []

        async def capturing_send(message: Message) -> None:
            nonlocal captured_status
            if message["type"] == "http.response.start":
                captured_status = message["status"]
            captured_response_parts.append(message)

        await self.app(scope, replay_receive, capturing_send)

        # Re-init if FastMCP returned 400 and session is in registry (cross-node)
        if captured_status == 400 and session is not None:
            logger.info(
                "Session %s in registry but not in FastMCP — re-initializing",
                session_id,
            )
            new_session_id = await self._reinitialize(scope, body, session, send)
            if new_session_id:
                return  # Re-init handled the response
            logger.warning("Re-initialization failed for session %s", session_id)

        # Send captured response to client (original or error)
        for part in captured_response_parts:
            await send(part)

        # Touch on success (debounced)
        if 200 <= captured_status < 300:
            self._debounced_touch(session_id)
            self.store._schedule_cleanup()

    async def _reinitialize(
        self,
        original_scope: Scope,
        original_body: bytes,
        session: dict[str, Any],
        client_send: Send,
    ) -> str | None:
        """Perform cross-node re-initialization.

        Sends a synthetic initialize to FastMCP, registers the new session,
        stores a redirect, invalidates the old session, and replays the
        original request.

        Returns the new session_id on success, None on failure.
        """
        old_session_id = session["session_id"]

        # Step 1: Synthetic initialize
        init_body = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": session.get("protocol_version", "2025-03-26"),
                    "capabilities": session.get("capabilities", {}),
                    "clientInfo": session.get("client_info", {}),
                },
            }
        ).encode()

        init_scope = dict(original_scope)
        init_scope["headers"] = [
            (k, v) for k, v in original_scope.get("headers", []) if k != b"mcp-session-id"
        ]

        init_status = 0
        init_headers: list[tuple[bytes, bytes]] = []

        async def init_receive() -> Message:
            return {"type": "http.request", "body": init_body}

        async def init_send(message: Message) -> None:
            nonlocal init_status, init_headers
            if message["type"] == "http.response.start":
                init_status = message["status"]
                init_headers = list(message.get("headers", []))

        await self.app(init_scope, init_receive, init_send)

        if init_status < 200 or init_status >= 300:
            return None

        new_session_id = self._extract_session_id(init_headers)
        if not new_session_id:
            return None

        # Step 2: Register new, redirect old→new, invalidate old
        owner_id = self._get_owner_id()
        try:
            self.store.register(
                session_id=new_session_id,
                owner_id=owner_id,
                node=self.node_name,
                protocol_version=session.get("protocol_version"),
                capabilities=session.get("capabilities", {}),
                client_info=session.get("client_info", {}),
            )
            self.store.add_redirect(old_session_id, new_session_id)
            self.store.invalidate(old_session_id)
            logger.info("Re-initialized session: %s → %s", old_session_id, new_session_id)
        except Exception:
            logger.error("Failed to persist re-init for %s", old_session_id, exc_info=True)
            return None  # Don't replay on an untracked session

        # Step 3: Replay original request with new session_id
        replay_scope = self._rewrite_session_header(original_scope, new_session_id)

        async def replay_receive() -> Message:
            return {"type": "http.request", "body": original_body}

        async def replay_send(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = [(k, v) for k, v in message.get("headers", []) if k != b"mcp-session-id"]
                headers.append((b"mcp-session-id", new_session_id.encode()))
                message = dict(message)
                message["headers"] = headers
            await client_send(message)

        await self.app(replay_scope, replay_receive, replay_send)
        return new_session_id

    async def _handle_terminate(
        self, scope: Scope, receive: Receive, send: Send, session_id: str | None
    ) -> None:
        """Handle DELETE /mcp: pass through to FastMCP, then clean up registry."""
        await self.app(scope, receive, send)

        if session_id:
            try:
                self.store.invalidate(session_id)
                self.store.delete_redirects_to(session_id)
                logger.info("Session terminated: %s", session_id)
                self.store._schedule_cleanup()
            except Exception:
                logger.error(
                    "Failed to clean up terminated session %s",
                    session_id,
                    exc_info=True,
                )

    def _debounced_touch(self, session_id: str) -> None:
        """Touch the session, debounced to once per 30 seconds."""
        now = time.monotonic()
        last = self._touch_debounce.get(session_id, 0.0)
        if now - last < self._touch_debounce_seconds:
            return
        self._touch_debounce[session_id] = now
        try:
            self.store.touch(session_id)
        except Exception:
            logger.error("Touch failed for session %s", session_id, exc_info=True)

    @staticmethod
    def _rewrite_session_header(scope: Scope, new_session_id: str) -> Scope:
        """Return a new scope with the mcp-session-id header rewritten."""
        new_scope = dict(scope)
        new_scope["headers"] = [
            (k, v) for k, v in scope.get("headers", []) if k != b"mcp-session-id"
        ]
        new_scope["headers"].append((b"mcp-session-id", new_session_id.encode()))
        return new_scope

    @staticmethod
    async def _send_error(send: Send, status: int, message: str) -> None:
        """Send a JSON error response."""
        body = json.dumps({"error": message}).encode()
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send({"type": "http.response.body", "body": body})

    @staticmethod
    async def _buffer_body(receive: Receive) -> tuple[bytes, Receive]:
        """Buffer the full request body and return (body_bytes, replay_receive)."""
        chunks: list[Message] = []
        body_parts: list[bytes] = []
        while True:
            message = await receive()
            chunks.append(message)
            if message.get("type") == "http.request":
                body_parts.append(message.get("body", b""))
                if not message.get("more_body", False):
                    break
            elif message.get("type") == "http.disconnect":
                break

        body = b"".join(body_parts)
        chunk_iter = iter(chunks)

        async def replay_receive() -> Message:
            try:
                return next(chunk_iter)
            except StopIteration:
                return {"type": "http.disconnect"}

        return body, replay_receive

    @staticmethod
    def _extract_session_id(headers: list[tuple[bytes, bytes]]) -> str | None:
        """Extract mcp-session-id from response headers."""
        for key, value in headers:
            if key == b"mcp-session-id":
                return value.decode()
        return None

    @staticmethod
    def _get_owner_id() -> str:
        """Get owner_id from contextvars (set by AuthMiddleware)."""
        from .server import _owner_id

        return _owner_id()
