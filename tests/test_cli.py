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

"""Tests for CLI entry points (mcp-awareness-secret, mcp-awareness-token, mcp-awareness-user)."""

from __future__ import annotations

import argparse
from datetime import timedelta
from io import StringIO
from unittest.mock import patch

import jwt
import pytest

from mcp_awareness.cli import (
    _canonical_email,
    _parse_duration,
    _validate_phone,
    secret_main,
    token_main,
)

# ---------------------------------------------------------------------------
# _canonical_email
# ---------------------------------------------------------------------------


class TestCanonicalEmail:
    def test_gmail_dots_stripped(self) -> None:
        assert _canonical_email("j.o.h.n@gmail.com") == "john@gmail.com"

    def test_plus_tag_stripped(self) -> None:
        assert _canonical_email("user+tag@example.com") == "user@example.com"

    def test_gmail_plus_and_dots(self) -> None:
        assert _canonical_email("j.doe+newsletter@gmail.com") == "jdoe@gmail.com"

    def test_googlemail_normalized(self) -> None:
        assert _canonical_email("user@googlemail.com") == "user@gmail.com"

    def test_non_gmail_preserves_dots(self) -> None:
        assert _canonical_email("j.doe@outlook.com") == "j.doe@outlook.com"

    def test_uppercase_normalized(self) -> None:
        assert _canonical_email("User@Example.COM") == "user@example.com"

    def test_whitespace_stripped(self) -> None:
        assert _canonical_email("  user@example.com  ") == "user@example.com"

    def test_no_domain(self) -> None:
        assert _canonical_email("localonly") == "localonly"


# ---------------------------------------------------------------------------
# _validate_phone
# ---------------------------------------------------------------------------


class TestValidatePhone:
    def test_valid_us_number(self) -> None:
        result = _validate_phone("+14155551234")
        assert result == "+14155551234"

    def test_valid_uk_number(self) -> None:
        result = _validate_phone("+447911123456")
        assert result.startswith("+44")

    def test_invalid_number(self) -> None:
        with pytest.raises(ValueError, match="Invalid phone number"):
            _validate_phone("+1999")

    def test_unparseable_number(self) -> None:
        with pytest.raises(ValueError, match="Cannot parse"):
            _validate_phone("not-a-number")


# ---------------------------------------------------------------------------
# _parse_duration
# ---------------------------------------------------------------------------


class TestParseDuration:
    def test_days(self) -> None:
        assert _parse_duration("30d") == timedelta(days=30)

    def test_hours(self) -> None:
        assert _parse_duration("24h") == timedelta(hours=24)

    def test_invalid_suffix(self) -> None:
        with pytest.raises(ValueError, match="Invalid duration"):
            _parse_duration("10m")


# ---------------------------------------------------------------------------
# secret_main
# ---------------------------------------------------------------------------


class TestSecretMain:
    def test_outputs_urlsafe_string(self, capsys: pytest.CaptureFixture[str]) -> None:
        secret_main()
        output = capsys.readouterr().out.strip()
        # secrets.token_urlsafe(32) produces 43 characters
        assert len(output) == 43
        # Should be URL-safe base64
        assert all(c.isalnum() or c in "-_" for c in output)


# ---------------------------------------------------------------------------
# token_main
# ---------------------------------------------------------------------------


class TestTokenMain:
    def test_generates_valid_jwt(self, monkeypatch: pytest.MonkeyPatch) -> None:
        secret = "test-token-secret"
        monkeypatch.setenv("AWARENESS_JWT_SECRET", secret)
        monkeypatch.setattr(
            "sys.argv", ["mcp-awareness-token", "--user", "alice", "--expires", "1d"]
        )
        buf = StringIO()
        with patch("sys.stdout", buf):
            token_main()
        token = buf.getvalue().strip()
        payload = jwt.decode(token, secret, algorithms=["HS256"])
        assert payload["sub"] == "alice"
        assert "exp" in payload

    def test_missing_secret_exits(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("AWARENESS_JWT_SECRET", raising=False)
        monkeypatch.setattr("sys.argv", ["mcp-awareness-token", "--user", "alice"])
        with pytest.raises(SystemExit):
            token_main()


# ---------------------------------------------------------------------------
# User CLI subcommands (requires database)
# ---------------------------------------------------------------------------


class TestUserAdd:
    def test_add_user(self, pg_dsn: str) -> None:
        """Add a user via _user_add and verify it exists."""
        import psycopg
        from psycopg.rows import dict_row

        from mcp_awareness.cli import _user_add

        # Ensure users table exists
        with psycopg.connect(pg_dsn) as conn, conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    id TEXT PRIMARY KEY,
                    email TEXT,
                    canonical_email TEXT UNIQUE,
                    email_verified BOOLEAN NOT NULL DEFAULT FALSE,
                    phone TEXT,
                    phone_verified BOOLEAN NOT NULL DEFAULT FALSE,
                    password_hash TEXT,
                    display_name TEXT,
                    timezone TEXT DEFAULT 'UTC',
                    preferences JSONB NOT NULL DEFAULT '{}',
                    created TIMESTAMPTZ NOT NULL DEFAULT now(),
                    updated TIMESTAMPTZ NOT NULL DEFAULT now(),
                    deleted TIMESTAMPTZ
                )
            """)

        args = argparse.Namespace(
            user_id="test-user-1",
            email="test@example.com",
            display_name="Test User",
            phone=None,
            timezone="America/New_York",
        )
        _user_add(pg_dsn, args)

        # Verify
        with psycopg.connect(pg_dsn, row_factory=dict_row) as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM users WHERE id = %s", ("test-user-1",))
            row = cur.fetchone()
        assert row is not None
        assert row["email"] == "test@example.com"
        assert row["canonical_email"] == "test@example.com"
        assert row["display_name"] == "Test User"
        assert row["timezone"] == "America/New_York"

        # Cleanup
        with psycopg.connect(pg_dsn) as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM users WHERE id = %s", ("test-user-1",))


class TestUserList:
    def test_list_users(self, pg_dsn: str, capsys: pytest.CaptureFixture[str]) -> None:
        """List users shows active users."""
        import psycopg

        from mcp_awareness.cli import _user_list

        # Ensure table and insert test user
        with psycopg.connect(pg_dsn) as conn, conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    id TEXT PRIMARY KEY,
                    email TEXT,
                    canonical_email TEXT UNIQUE,
                    email_verified BOOLEAN NOT NULL DEFAULT FALSE,
                    phone TEXT,
                    phone_verified BOOLEAN NOT NULL DEFAULT FALSE,
                    password_hash TEXT,
                    display_name TEXT,
                    timezone TEXT DEFAULT 'UTC',
                    preferences JSONB NOT NULL DEFAULT '{}',
                    created TIMESTAMPTZ NOT NULL DEFAULT now(),
                    updated TIMESTAMPTZ NOT NULL DEFAULT now(),
                    deleted TIMESTAMPTZ
                )
            """)
            cur.execute(
                "INSERT INTO users (id, email, display_name) "
                "VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                ("list-user", "list@example.com", "List User"),
            )

        _user_list(pg_dsn)
        output = capsys.readouterr().out
        assert "list-user" in output

        # Cleanup
        with psycopg.connect(pg_dsn) as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM users WHERE id = %s", ("list-user",))


class TestUserDelete:
    def test_delete_without_confirm_exits(self, pg_dsn: str) -> None:
        """Delete without --confirm exits with error."""
        from mcp_awareness.cli import _user_delete

        args = argparse.Namespace(user_id="someone", confirm=False)
        with pytest.raises(SystemExit):
            _user_delete(pg_dsn, args)

    def test_delete_with_confirm(self, pg_dsn: str, store: object) -> None:
        """Delete with --confirm removes user and data.

        Uses the store fixture to ensure all tables (entries, reads, actions, etc.) exist.
        """
        import psycopg

        from mcp_awareness.cli import _user_delete

        # Ensure users table and insert test user
        with psycopg.connect(pg_dsn) as conn, conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    id TEXT PRIMARY KEY,
                    email TEXT,
                    canonical_email TEXT UNIQUE,
                    email_verified BOOLEAN NOT NULL DEFAULT FALSE,
                    phone TEXT,
                    phone_verified BOOLEAN NOT NULL DEFAULT FALSE,
                    password_hash TEXT,
                    display_name TEXT,
                    timezone TEXT DEFAULT 'UTC',
                    preferences JSONB NOT NULL DEFAULT '{}',
                    created TIMESTAMPTZ NOT NULL DEFAULT now(),
                    updated TIMESTAMPTZ NOT NULL DEFAULT now(),
                    deleted TIMESTAMPTZ
                )
            """)
            cur.execute(
                "INSERT INTO users (id, email) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                ("delete-user", "delete@example.com"),
            )

        args = argparse.Namespace(user_id="delete-user", confirm=True)
        _user_delete(pg_dsn, args)

        # Verify deleted
        from psycopg.rows import dict_row

        with psycopg.connect(pg_dsn, row_factory=dict_row) as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM users WHERE id = %s", ("delete-user",))
            assert cur.fetchone() is None
