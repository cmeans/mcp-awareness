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

"""Tests for mcp-awareness-register-schema CLI."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest


@pytest.fixture
def system_schema_file():
    with tempfile.NamedTemporaryFile(suffix=".json", mode="w", delete=False) as f:
        json.dump({"type": "object", "properties": {"name": {"type": "string"}}}, f)
        path = f.name
    yield path
    Path(path).unlink(missing_ok=True)


def test_cli_register_schema_happy_path(pg_dsn, system_schema_file, monkeypatch, capsys):
    """End-to-end: CLI writes a _system schema via direct store access."""
    from mcp_awareness.cli_register_schema import main

    monkeypatch.setenv("AWARENESS_DATABASE_URL", pg_dsn)
    monkeypatch.setattr(
        "sys.argv",
        [
            "mcp-awareness-register-schema",
            "--system",
            "--family",
            "schema:cli-test",
            "--version",
            "1.0.0",
            "--schema-file",
            system_schema_file,
            "--source",
            "awareness-built-in",
            "--tags",
            "cli,test",
            "--description",
            "CLI-registered test schema",
        ],
    )

    # Seed _system user so insert doesn't FK-violate (conftest fixture does this for store tests;
    # CLI creates its own PostgresStore so we seed manually here)
    from mcp_awareness.postgres_store import PostgresStore

    tmp = PostgresStore(pg_dsn)
    with tmp._pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO users (id, display_name) VALUES ('_system', 'System-managed schemas') "
            "ON CONFLICT (id) DO NOTHING"
        )
        conn.commit()

    main()
    captured = capsys.readouterr()
    body = json.loads(captured.out.strip())
    assert body["status"] == "ok"
    assert body["logical_key"] == "schema:cli-test:1.0.0"

    # Verify entry exists in DB under _system owner
    store = PostgresStore(pg_dsn)
    entry = store.find_schema("any-caller", "schema:cli-test:1.0.0")
    assert entry is not None
    assert entry.data["learned_from"] == "cli-bootstrap"


def test_cli_register_schema_rejects_invalid_schema_file(pg_dsn, monkeypatch, capsys):
    from mcp_awareness.cli_register_schema import main

    with tempfile.NamedTemporaryFile(suffix=".json", mode="w", delete=False) as f:
        json.dump({"type": "strng"}, f)  # invalid
        path = f.name

    monkeypatch.setenv("AWARENESS_DATABASE_URL", pg_dsn)
    monkeypatch.setattr(
        "sys.argv",
        [
            "mcp-awareness-register-schema",
            "--system",
            "--family",
            "schema:bad",
            "--version",
            "1.0.0",
            "--schema-file",
            path,
            "--source",
            "test",
            "--tags",
            "",
            "--description",
            "bad",
        ],
    )
    with pytest.raises(SystemExit) as excinfo:
        main()
    assert excinfo.value.code == 1
    captured = capsys.readouterr()
    assert "invalid_schema" in captured.err
    Path(path).unlink(missing_ok=True)


def test_cli_register_schema_missing_db_url(monkeypatch, system_schema_file, capsys):
    from mcp_awareness.cli_register_schema import main

    monkeypatch.delenv("AWARENESS_DATABASE_URL", raising=False)
    monkeypatch.setattr(
        "sys.argv",
        [
            "mcp-awareness-register-schema",
            "--system",
            "--family",
            "schema:test",
            "--version",
            "1.0.0",
            "--schema-file",
            system_schema_file,
            "--source",
            "test",
            "--tags",
            "",
            "--description",
            "test",
        ],
    )
    with pytest.raises(SystemExit) as excinfo:
        main()
    assert excinfo.value.code == 1
    captured = capsys.readouterr()
    assert "AWARENESS_DATABASE_URL" in captured.err or "missing_env" in captured.err


def test_cli_register_schema_missing_schema_file(pg_dsn, monkeypatch, capsys):
    from mcp_awareness.cli_register_schema import main

    monkeypatch.setenv("AWARENESS_DATABASE_URL", pg_dsn)
    monkeypatch.setattr(
        "sys.argv",
        [
            "mcp-awareness-register-schema",
            "--system",
            "--family",
            "schema:test",
            "--version",
            "1.0.0",
            "--schema-file",
            "/nonexistent/path.json",
            "--source",
            "test",
            "--tags",
            "",
            "--description",
            "test",
        ],
    )
    with pytest.raises(SystemExit) as excinfo:
        main()
    assert excinfo.value.code == 1
