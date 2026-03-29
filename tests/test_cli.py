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
from typing import Any
from unittest.mock import patch

import jwt
import pytest

from mcp_awareness.cli import (
    _canonical_email,
    _get_dsn,
    _parse_duration,
    _user_add,
    _user_delete,
    _user_export,
    _user_list,
    _user_set_password,
    _validate_phone,
    secret_main,
    token_main,
    user_main,
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

    def test_missing_secret_exits(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.delenv("AWARENESS_JWT_SECRET", raising=False)
        monkeypatch.setattr("sys.argv", ["mcp-awareness-token", "--user", "alice"])
        with pytest.raises(SystemExit) as exc_info:
            token_main()
        assert exc_info.value.code == 1
        assert "AWARENESS_JWT_SECRET" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# _get_dsn
# ---------------------------------------------------------------------------


class TestGetDsn:
    def test_returns_dsn_when_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AWARENESS_DATABASE_URL", "postgresql://localhost/test")
        assert _get_dsn() == "postgresql://localhost/test"

    def test_exits_when_missing(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.delenv("AWARENESS_DATABASE_URL", raising=False)
        with pytest.raises(SystemExit) as exc_info:
            _get_dsn()
        assert exc_info.value.code == 1
        assert "AWARENESS_DATABASE_URL" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# User CLI subcommands (requires database)
# ---------------------------------------------------------------------------


_USERS_DDL = """
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
    updated TIMESTAMPTZ,
    deleted TIMESTAMPTZ
)
"""


def _ensure_users_table(dsn: str) -> None:
    """Create users table if it doesn't exist."""
    import psycopg

    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(_USERS_DDL)


def _insert_user(dsn: str, user_id: str, email: str = "u@example.com") -> None:
    """Insert a test user."""
    import psycopg

    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO users (id, email) VALUES (%s, %s) ON CONFLICT DO NOTHING",
            (user_id, email),
        )


def _cleanup_user(dsn: str, user_id: str) -> None:
    """Remove a test user."""
    import psycopg

    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM users WHERE id = %s", (user_id,))


class TestUserAdd:
    def test_add_user(self, pg_dsn: str) -> None:
        """Add a user via _user_add and verify it exists."""
        import psycopg
        from psycopg.rows import dict_row

        _ensure_users_table(pg_dsn)

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

        _cleanup_user(pg_dsn, "test-user-1")

    def test_add_user_with_phone(self, pg_dsn: str) -> None:
        """Add a user with a valid phone number — validates and stores E.164."""
        import psycopg
        from psycopg.rows import dict_row

        _ensure_users_table(pg_dsn)

        args = argparse.Namespace(
            user_id="phone-user",
            email="phone@example.com",
            display_name=None,
            phone="+14155551234",
            timezone="UTC",
        )
        _user_add(pg_dsn, args)

        with psycopg.connect(pg_dsn, row_factory=dict_row) as conn, conn.cursor() as cur:
            cur.execute("SELECT phone FROM users WHERE id = %s", ("phone-user",))
            row = cur.fetchone()
        assert row is not None
        assert row["phone"] == "+14155551234"

        _cleanup_user(pg_dsn, "phone-user")

    def test_add_user_with_invalid_phone(self, pg_dsn: str) -> None:
        """Add a user with an invalid phone number raises ValueError."""
        _ensure_users_table(pg_dsn)

        args = argparse.Namespace(
            user_id="bad-phone-user",
            email=None,
            display_name=None,
            phone="not-a-number",
            timezone="UTC",
        )
        with pytest.raises(ValueError, match="Cannot parse"):
            _user_add(pg_dsn, args)

    def test_add_user_with_email_canonical(self, pg_dsn: str) -> None:
        """Add a user with email — canonical_email is computed."""
        import psycopg
        from psycopg.rows import dict_row

        _ensure_users_table(pg_dsn)

        args = argparse.Namespace(
            user_id="canonical-user",
            email="J.Doe+tag@gmail.com",
            display_name=None,
            phone=None,
            timezone="UTC",
        )
        _user_add(pg_dsn, args)

        with psycopg.connect(pg_dsn, row_factory=dict_row) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT email, canonical_email FROM users WHERE id = %s",
                ("canonical-user",),
            )
            row = cur.fetchone()
        assert row is not None
        assert row["email"] == "J.Doe+tag@gmail.com"
        assert row["canonical_email"] == "jdoe@gmail.com"

        _cleanup_user(pg_dsn, "canonical-user")


class TestUserList:
    def test_list_users(self, pg_dsn: str, capsys: pytest.CaptureFixture[str]) -> None:
        """List users shows active users."""
        _ensure_users_table(pg_dsn)

        import psycopg

        with psycopg.connect(pg_dsn) as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO users (id, email, display_name) "
                "VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                ("list-user", "list@example.com", "List User"),
            )

        _user_list(pg_dsn)
        output = capsys.readouterr().out
        assert "list-user" in output
        assert "<list@example.com>" in output
        assert "(List User)" in output

        _cleanup_user(pg_dsn, "list-user")

    def test_list_users_empty(self, pg_dsn: str, capsys: pytest.CaptureFixture[str]) -> None:
        """List users when no users exist prints a message."""
        _ensure_users_table(pg_dsn)

        import psycopg

        # Make sure the table is empty
        with psycopg.connect(pg_dsn) as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM users")

        _user_list(pg_dsn)
        output = capsys.readouterr().out
        assert "No users found" in output


class TestUserSetPassword:
    def test_set_password(self, pg_dsn: str, capsys: pytest.CaptureFixture[str]) -> None:
        """Set password hashes with argon2 and updates DB."""
        _ensure_users_table(pg_dsn)
        _insert_user(pg_dsn, "pw-user", "pw@example.com")

        args = argparse.Namespace(user_id="pw-user")
        strong_pw = "Tr0ub4dor&Horse99!"
        with patch("mcp_awareness.cli.getpass.getpass", side_effect=[strong_pw, strong_pw]):
            _user_set_password(pg_dsn, args)

        output = capsys.readouterr().out
        assert "Password set for 'pw-user'" in output

        # Verify hash was stored
        import psycopg
        from psycopg.rows import dict_row

        with psycopg.connect(pg_dsn, row_factory=dict_row) as conn, conn.cursor() as cur:
            cur.execute("SELECT password_hash FROM users WHERE id = %s", ("pw-user",))
            row = cur.fetchone()
        assert row is not None
        assert row["password_hash"] is not None
        assert row["password_hash"].startswith("$argon2")

        _cleanup_user(pg_dsn, "pw-user")

    def test_set_password_mismatch(self, pg_dsn: str, capsys: pytest.CaptureFixture[str]) -> None:
        """Mismatched passwords exit with error."""
        _ensure_users_table(pg_dsn)
        _insert_user(pg_dsn, "pw-mismatch", "pwm@example.com")

        args = argparse.Namespace(user_id="pw-mismatch")
        with (
            patch(
                "mcp_awareness.cli.getpass.getpass",
                side_effect=["password1", "password2"],
            ),
            pytest.raises(SystemExit) as exc_info,
        ):
            _user_set_password(pg_dsn, args)
        assert exc_info.value.code == 1
        assert "passwords do not match" in capsys.readouterr().err

        _cleanup_user(pg_dsn, "pw-mismatch")

    def test_set_password_user_not_found(
        self, pg_dsn: str, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Setting password for nonexistent user exits with error."""
        _ensure_users_table(pg_dsn)

        args = argparse.Namespace(user_id="no-such-user")
        strong_pw = "Tr0ub4dor&Horse99!"
        with (
            patch(
                "mcp_awareness.cli.getpass.getpass",
                side_effect=[strong_pw, strong_pw],
            ),
            pytest.raises(SystemExit) as exc_info,
        ):
            _user_set_password(pg_dsn, args)
        assert exc_info.value.code == 1
        assert "not found" in capsys.readouterr().err

    def test_set_password_too_short(
        self, pg_dsn: str, store: object, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Password under 14 chars is rejected."""
        _ensure_users_table(pg_dsn)
        _insert_user(pg_dsn, "short-pw-user")
        args = argparse.Namespace(user_id="short-pw-user")
        with (
            patch(
                "mcp_awareness.cli.getpass.getpass",
                side_effect=["short", "short"],
            ),
            pytest.raises(SystemExit) as exc_info,
        ):
            _user_set_password(pg_dsn, args)
        assert exc_info.value.code == 1
        assert "at least 14" in capsys.readouterr().err
        _cleanup_user(pg_dsn, "short-pw-user")

    def test_set_password_too_long(
        self, pg_dsn: str, store: object, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Password over 128 chars is rejected."""
        _ensure_users_table(pg_dsn)
        _insert_user(pg_dsn, "long-pw-user")
        args = argparse.Namespace(user_id="long-pw-user")
        long_pw = "A" * 129
        with (
            patch(
                "mcp_awareness.cli.getpass.getpass",
                side_effect=[long_pw, long_pw],
            ),
            pytest.raises(SystemExit) as exc_info,
        ):
            _user_set_password(pg_dsn, args)
        assert exc_info.value.code == 1
        assert "128 characters" in capsys.readouterr().err
        _cleanup_user(pg_dsn, "long-pw-user")

    def test_set_password_weak_zxcvbn(
        self, pg_dsn: str, store: object, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Password that meets length but is weak (zxcvbn < 3) is rejected."""
        _ensure_users_table(pg_dsn)
        _insert_user(pg_dsn, "weak-pw-user")
        args = argparse.Namespace(user_id="weak-pw-user")
        with (
            patch(
                "mcp_awareness.cli.getpass.getpass",
                side_effect=["aaaaaaaaaaaaaaaa", "aaaaaaaaaaaaaaaa"],
            ),
            pytest.raises(SystemExit) as exc_info,
        ):
            _user_set_password(pg_dsn, args)
        assert exc_info.value.code == 1
        assert "too weak" in capsys.readouterr().err
        _cleanup_user(pg_dsn, "weak-pw-user")


class TestUserExport:
    def test_export_to_stdout(
        self, pg_dsn: str, store: object, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Export user data as JSON to stdout."""
        import json

        _ensure_users_table(pg_dsn)
        _insert_user(pg_dsn, "export-user", "export@example.com")

        args = argparse.Namespace(user_id="export-user", output="-")
        _user_export(pg_dsn, args)

        output = capsys.readouterr().out
        data = json.loads(output)
        assert data["user_id"] == "export-user"
        assert data["user"]["email"] == "export@example.com"
        assert "entries" in data
        assert "reads" in data
        assert "actions" in data

        _cleanup_user(pg_dsn, "export-user")

    def test_export_to_file(
        self, pg_dsn: str, store: object, tmp_path: Any, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Export user data to a file."""
        import json

        _ensure_users_table(pg_dsn)
        _insert_user(pg_dsn, "export-file-user", "expfile@example.com")

        outfile = str(tmp_path / "export.json")
        args = argparse.Namespace(user_id="export-file-user", output=outfile)
        _user_export(pg_dsn, args)

        output = capsys.readouterr().out
        assert f"Exported to {outfile}" in output

        with open(outfile) as f:
            data = json.loads(f.read())
        assert data["user_id"] == "export-file-user"

        _cleanup_user(pg_dsn, "export-file-user")

    def test_export_with_entries_reads_actions(
        self, pg_dsn: str, store: object, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Export serializes datetime fields in entries, reads, and actions."""
        import json
        import uuid

        import psycopg

        _ensure_users_table(pg_dsn)
        _insert_user(pg_dsn, "export-full", "full@example.com")

        entry_id = str(uuid.uuid4())
        with psycopg.connect(pg_dsn) as conn, conn.cursor() as cur:
            cur.execute(
                """INSERT INTO entries (id, owner_id, type, source, created, updated, tags, data)
                   VALUES (%s, %s, 'note', 'test', now(), now(), '[]'::jsonb, '{}'::jsonb)""",
                (entry_id, "export-full"),
            )
            cur.execute(
                """INSERT INTO reads (owner_id, entry_id, timestamp, platform)
                   VALUES (%s, %s, now(), 'test')""",
                ("export-full", entry_id),
            )
            cur.execute(
                """INSERT INTO actions (owner_id, entry_id, timestamp, action, detail)
                   VALUES (%s, %s, now(), 'dismissed', 'test action')""",
                ("export-full", entry_id),
            )

        args = argparse.Namespace(user_id="export-full", output="-")
        _user_export(pg_dsn, args)

        output = capsys.readouterr().out
        data = json.loads(output)

        # Entries present with ISO timestamps
        assert len(data["entries"]) == 1
        entry = data["entries"][0]
        assert entry["id"] == entry_id
        assert isinstance(entry["created"], str)
        assert "T" in entry["created"]  # ISO format

        # Reads present with ISO timestamp
        assert len(data["reads"]) == 1
        read = data["reads"][0]
        assert isinstance(read["timestamp"], str)
        assert "T" in read["timestamp"]

        # Actions present with ISO timestamp
        assert len(data["actions"]) == 1
        action = data["actions"][0]
        assert isinstance(action["timestamp"], str)
        assert "T" in action["timestamp"]

        _cleanup_user(pg_dsn, "export-full")

    def test_export_user_not_found(
        self, pg_dsn: str, store: object, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Export nonexistent user exits with error."""
        _ensure_users_table(pg_dsn)

        args = argparse.Namespace(user_id="no-such-export", output="-")
        with pytest.raises(SystemExit) as exc_info:
            _user_export(pg_dsn, args)
        assert exc_info.value.code == 1
        assert "not found" in capsys.readouterr().err


class TestUserDelete:
    def test_delete_without_confirm_exits(
        self, pg_dsn: str, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Delete without --confirm exits with dry-run message."""
        args = argparse.Namespace(user_id="someone", confirm=False)
        with pytest.raises(SystemExit) as exc_info:
            _user_delete(pg_dsn, args)
        assert exc_info.value.code == 1
        err = capsys.readouterr().err
        assert "permanently delete" in err
        assert "Re-run with --confirm" in err

    def test_delete_with_confirm(
        self, pg_dsn: str, store: object, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Delete with --confirm removes user, entries, reads, and actions."""
        import uuid

        import psycopg
        from psycopg.rows import dict_row

        _ensure_users_table(pg_dsn)

        entry_id = str(uuid.uuid4())
        with psycopg.connect(pg_dsn) as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO users (id, email) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                ("delete-user", "delete@example.com"),
            )
            cur.execute(
                """INSERT INTO entries (id, owner_id, type, source, created, updated, tags, data)
                   VALUES (%s, %s, 'note', 'test', now(), now(), '[]'::jsonb, '{}'::jsonb)""",
                (entry_id, "delete-user"),
            )
            cur.execute(
                """INSERT INTO reads (owner_id, entry_id, timestamp, platform)
                   VALUES (%s, %s, now(), 'test')""",
                ("delete-user", entry_id),
            )
            cur.execute(
                """INSERT INTO actions (owner_id, entry_id, timestamp, action, detail)
                   VALUES (%s, %s, now(), 'dismissed', 'test action')""",
                ("delete-user", entry_id),
            )

        args = argparse.Namespace(user_id="delete-user", confirm=True)
        _user_delete(pg_dsn, args)

        output = capsys.readouterr().out
        assert "1 entries" in output
        assert "1 reads" in output
        assert "1 actions" in output

        with psycopg.connect(pg_dsn, row_factory=dict_row) as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM users WHERE id = %s", ("delete-user",))
            assert cur.fetchone() is None
            cur.execute("SELECT * FROM entries WHERE id = %s", (entry_id,))
            assert cur.fetchone() is None
            cur.execute("SELECT * FROM reads WHERE entry_id = %s", (entry_id,))
            assert cur.fetchone() is None
            cur.execute("SELECT * FROM actions WHERE entry_id = %s", (entry_id,))
            assert cur.fetchone() is None

    def test_delete_nonexistent_user(
        self, pg_dsn: str, store: object, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Delete a user that doesn't exist exits with error."""
        _ensure_users_table(pg_dsn)

        args = argparse.Namespace(user_id="ghost-user", confirm=True)
        with pytest.raises(SystemExit) as exc_info:
            _user_delete(pg_dsn, args)
        assert exc_info.value.code == 1
        assert "not found" in capsys.readouterr().err


class TestUserMainDispatch:
    """Test user_main() argparse dispatch covers all subcommands."""

    def test_add_via_main(
        self, pg_dsn: str, store: object, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _ensure_users_table(pg_dsn)
        monkeypatch.setenv("AWARENESS_DATABASE_URL", pg_dsn)
        monkeypatch.setattr("sys.argv", ["prog", "add", "main-test-user"])
        user_main()
        import psycopg
        from psycopg.rows import dict_row

        with psycopg.connect(pg_dsn, row_factory=dict_row) as conn, conn.cursor() as cur:
            cur.execute("SELECT id FROM users WHERE id = %s", ("main-test-user",))
            assert cur.fetchone() is not None
            cur.execute("DELETE FROM users WHERE id = %s", ("main-test-user",))

    def test_list_via_main(
        self, pg_dsn: str, store: object, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _ensure_users_table(pg_dsn)
        monkeypatch.setenv("AWARENESS_DATABASE_URL", pg_dsn)
        monkeypatch.setattr("sys.argv", ["prog", "list"])
        user_main()

    def test_export_via_main(
        self,
        pg_dsn: str,
        store: object,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _ensure_users_table(pg_dsn)
        _insert_user(pg_dsn, "export-main-user")
        monkeypatch.setenv("AWARENESS_DATABASE_URL", pg_dsn)
        monkeypatch.setattr("sys.argv", ["prog", "export", "export-main-user"])
        user_main()
        output = capsys.readouterr().out
        assert "export-main-user" in output
        _cleanup_user(pg_dsn, "export-main-user")

    def test_delete_via_main(
        self, pg_dsn: str, store: object, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _ensure_users_table(pg_dsn)
        _insert_user(pg_dsn, "delete-main-user")
        monkeypatch.setenv("AWARENESS_DATABASE_URL", pg_dsn)
        monkeypatch.setattr("sys.argv", ["prog", "delete", "delete-main-user", "--confirm"])
        user_main()
        import psycopg
        from psycopg.rows import dict_row

        with psycopg.connect(pg_dsn, row_factory=dict_row) as conn, conn.cursor() as cur:
            cur.execute("SELECT id FROM users WHERE id = %s", ("delete-main-user",))
            assert cur.fetchone() is None

    def test_set_password_via_main(
        self, pg_dsn: str, store: object, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _ensure_users_table(pg_dsn)
        _insert_user(pg_dsn, "pw-main-user")
        monkeypatch.setenv("AWARENESS_DATABASE_URL", pg_dsn)
        monkeypatch.setattr("sys.argv", ["prog", "set-password", "pw-main-user"])
        monkeypatch.setattr("getpass.getpass", lambda prompt="": "Tr0ub4dor&Horse99!")
        user_main()
        import psycopg
        from psycopg.rows import dict_row

        with psycopg.connect(pg_dsn, row_factory=dict_row) as conn, conn.cursor() as cur:
            cur.execute("SELECT password_hash FROM users WHERE id = %s", ("pw-main-user",))
            row = cur.fetchone()
            assert row is not None
            assert row["password_hash"] is not None
            cur.execute("DELETE FROM users WHERE id = %s", ("pw-main-user",))
