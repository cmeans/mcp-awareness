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

"""Shared test fixtures — PostgresStore via testcontainers."""

from __future__ import annotations

import os

import pytest
from testcontainers.postgres import PostgresContainer

from mcp_awareness.postgres_store import PostgresStore

TEST_OWNER = "test-owner"

# Set default owner for all tests before any module imports read it.
os.environ["AWARENESS_DEFAULT_OWNER"] = TEST_OWNER

# testcontainers needs to find the Docker socket. Docker Desktop on Linux
# uses a non-default path — set DOCKER_HOST if not already configured.
_DOCKER_DESKTOP_SOCK = os.path.expanduser("~/.docker/desktop/docker.sock")
if not os.environ.get("DOCKER_HOST") and os.path.exists(_DOCKER_DESKTOP_SOCK):
    os.environ["DOCKER_HOST"] = f"unix://{_DOCKER_DESKTOP_SOCK}"

# Disable the Reaper (ryuk) container — avoids compatibility issues with
# Docker Desktop and keeps CI simpler. Containers are cleaned up by scope exit.
os.environ.setdefault("TESTCONTAINERS_RYUK_DISABLED", "true")


@pytest.fixture(scope="session")
def pg_container():
    """Start a Postgres container once for the entire test session."""
    with PostgresContainer("pgvector/pgvector:pg17").with_env(
        "POSTGRES_INITDB_ARGS",
        "--encoding=UTF8 --lc-collate=C.UTF-8 --lc-ctype=C.UTF-8",
    ) as pg:
        yield pg


@pytest.fixture
def pg_dsn(pg_container):
    """Connection string for the session-scoped Postgres container."""
    url = pg_container.get_connection_url()
    # testcontainers returns sqlalchemy-style URL (postgresql+psycopg2://...)
    # psycopg needs plain postgresql:// format
    return url.replace("postgresql+psycopg2://", "postgresql://")


@pytest.fixture
def store(pg_dsn):
    """Fresh PostgresStore for each test — tables created, then cleared after."""
    s = PostgresStore(pg_dsn)
    yield s
    s.clear(TEST_OWNER)
