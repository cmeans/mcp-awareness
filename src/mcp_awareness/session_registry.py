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
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

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

    def close(self) -> None:
        """Close the connection pool."""
        self._pool.close()
