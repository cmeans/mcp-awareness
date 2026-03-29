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

"""CLI entry points for user management, token generation, and secret generation."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from typing import Any

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_duration(s: str) -> timedelta:
    """Parse '30d', '24h', '90d' etc."""
    if s.endswith("d"):
        return timedelta(days=int(s[:-1]))
    if s.endswith("h"):
        return timedelta(hours=int(s[:-1]))
    raise ValueError(f"Invalid duration: {s}. Use format like '30d' or '24h'")


def _canonical_email(email: str) -> str:
    """Normalize email for uniqueness: strip +tags, dots for gmail, lowercase."""
    email = email.lower().strip()
    local, _, domain = email.partition("@")
    if not domain:
        return email
    # Strip +tag
    local = local.split("+")[0]
    # Gmail/Googlemail: strip dots
    if domain in ("gmail.com", "googlemail.com"):
        local = local.replace(".", "")
        domain = "gmail.com"
    return f"{local}@{domain}"


def _validate_phone(phone: str) -> str:
    """Validate and format phone to E.164."""
    import phonenumbers

    try:
        parsed = phonenumbers.parse(phone, None)
        if not phonenumbers.is_valid_number(parsed):
            raise ValueError(f"Invalid phone number: {phone}")
        return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)
    except phonenumbers.NumberParseException as e:
        raise ValueError(f"Cannot parse phone number: {phone}") from e


def _get_dsn() -> str:
    """Get DSN from environment or exit with error."""
    dsn = os.environ.get("AWARENESS_DATABASE_URL")
    if not dsn:
        print("Error: AWARENESS_DATABASE_URL is required", file=sys.stderr)
        sys.exit(1)
    return dsn


# ---------------------------------------------------------------------------
# mcp-awareness-secret
# ---------------------------------------------------------------------------


def secret_main() -> None:
    """Generate a 256-bit JWT signing secret."""
    import secrets

    print(secrets.token_urlsafe(32))


# ---------------------------------------------------------------------------
# mcp-awareness-token
# ---------------------------------------------------------------------------


def token_main() -> None:
    """Generate a JWT for a user."""
    import jwt

    parser = argparse.ArgumentParser(description="Generate a JWT for mcp-awareness")
    parser.add_argument("--user", required=True, help="User ID (owner_id)")
    parser.add_argument("--expires", default="30d", help="Expiry (e.g., 30d, 24h, 90d)")
    args = parser.parse_args()

    secret = os.environ.get("AWARENESS_JWT_SECRET")
    if not secret:
        print("Error: AWARENESS_JWT_SECRET environment variable is required", file=sys.stderr)
        sys.exit(1)

    algorithm = os.environ.get("AWARENESS_JWT_ALGORITHM", "HS256")

    # Parse expiry
    duration = _parse_duration(args.expires)
    now = datetime.now(timezone.utc)
    payload = {
        "sub": args.user,
        "iat": now,
        "exp": now + duration,
    }
    token: str = jwt.encode(payload, secret, algorithm=algorithm)
    print(token)


# ---------------------------------------------------------------------------
# mcp-awareness-user subcommands
# ---------------------------------------------------------------------------


def _user_add(dsn: str, args: argparse.Namespace) -> None:
    """Add a new user."""
    import psycopg

    canonical = None
    if args.email:
        canonical = _canonical_email(args.email)

    phone = None
    if args.phone:
        phone = _validate_phone(args.phone)

    now = datetime.now(timezone.utc)
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO users
                (id, email, canonical_email, phone, display_name, timezone, created)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (
                args.user_id,
                args.email,
                canonical,
                phone,
                args.display_name,
                args.timezone,
                now,
            ),
        )
    print(f"User '{args.user_id}' created.")


def _user_list(dsn: str) -> None:
    """List all active users."""
    import psycopg
    from psycopg.rows import dict_row

    with psycopg.connect(dsn, row_factory=dict_row) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id, email, display_name, timezone, created FROM users WHERE deleted IS NULL"
        )
        rows = cur.fetchall()
    if not rows:
        print("No users found.")
        return
    for row in rows:
        parts = [row["id"]]
        if row.get("email"):
            parts.append(f"<{row['email']}>")
        if row.get("display_name"):
            parts.append(f"({row['display_name']})")
        parts.append(f"tz={row.get('timezone', 'UTC')}")
        print("  ".join(parts))


def _user_set_password(dsn: str, args: argparse.Namespace) -> None:
    """Set a user's password (interactive prompt)."""
    import psycopg
    from argon2 import PasswordHasher
    from zxcvbn import zxcvbn

    max_attempts = 3
    print("Password requirements: 14-128 characters, must be strong (no common patterns).")
    for attempt in range(1, max_attempts + 1):
        password = getpass.getpass(f"New password for '{args.user_id}': ")

        # Validate strength before asking for confirmation
        issues: list[str] = []
        if len(password) > 128:
            issues.append("Must be 128 characters or fewer.")
        if len(password) < 14:
            issues.append(f"Must be at least 14 characters (got {len(password)}).")

        if not issues:
            result = zxcvbn(password, user_inputs=[args.user_id])
            if result["score"] < 3:
                warning = result["feedback"].get("warning", "")
                if warning:
                    issues.append(warning)
                for s in result["feedback"].get("suggestions", []):
                    issues.append(s)
                if not issues:
                    issues.append("Password is too easily guessed.")

        if issues:
            print("Password does not meet requirements:", file=sys.stderr)
            for issue in issues:
                print(f"  · {issue}", file=sys.stderr)
            if attempt < max_attempts:
                print(f"Please try again ({max_attempts - attempt} attempt(s) remaining).")
                continue
            print("Too many failed attempts.", file=sys.stderr)
            sys.exit(1)

        # Password is strong — now confirm
        confirm = getpass.getpass("Confirm password: ")
        if password != confirm:
            print("Passwords do not match.", file=sys.stderr)
            if attempt < max_attempts:
                print(f"Please try again ({max_attempts - attempt} attempt(s) remaining).")
                continue
            print("Too many failed attempts.", file=sys.stderr)
            sys.exit(1)

        break  # password accepted

    ph = PasswordHasher(time_cost=3)
    hashed = ph.hash(password)
    now = datetime.now(timezone.utc)
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE users SET password_hash = %s, updated = %s WHERE id = %s AND deleted IS NULL",
            (hashed, now, args.user_id),
        )
        if cur.rowcount == 0:
            print(f"Error: user '{args.user_id}' not found", file=sys.stderr)
            sys.exit(1)
    print(f"Password set for '{args.user_id}'.")


def _user_export(dsn: str, args: argparse.Namespace) -> None:
    """Export all user data as JSON (GDPR)."""
    import psycopg
    from psycopg.rows import dict_row

    data: dict[str, Any] = {"user_id": args.user_id}

    with psycopg.connect(dsn, row_factory=dict_row) as conn, conn.cursor() as cur:
        # User record
        cur.execute("SELECT * FROM users WHERE id = %s", (args.user_id,))
        user_row = cur.fetchone()
        if user_row is None:
            print(f"Error: user '{args.user_id}' not found", file=sys.stderr)
            sys.exit(1)
        # Convert datetime fields to ISO strings
        for k, v in user_row.items():
            if isinstance(v, datetime):
                user_row[k] = v.isoformat()
        data["user"] = user_row

        # Entries
        cur.execute(
            "SELECT * FROM entries WHERE owner_id = %s ORDER BY created",
            (args.user_id,),
        )
        entries = cur.fetchall()
        for e in entries:
            for k, v in e.items():
                if isinstance(v, datetime):
                    e[k] = v.isoformat()
        data["entries"] = entries

        # Reads
        cur.execute(
            "SELECT * FROM reads WHERE owner_id = %s ORDER BY timestamp",
            (args.user_id,),
        )
        reads = cur.fetchall()
        for r in reads:
            for k, v in r.items():
                if isinstance(v, datetime):
                    r[k] = v.isoformat()
        data["reads"] = reads

        # Actions
        cur.execute(
            "SELECT * FROM actions WHERE owner_id = %s ORDER BY timestamp",
            (args.user_id,),
        )
        actions = cur.fetchall()
        for a in actions:
            for k, v in a.items():
                if isinstance(v, datetime):
                    a[k] = v.isoformat()
        data["actions"] = actions

    output = json.dumps(data, indent=2, default=str)
    if args.output == "-":
        print(output)
    else:
        with open(args.output, "w") as f:
            f.write(output)
        print(f"Exported to {args.output}")


def _user_delete(dsn: str, args: argparse.Namespace) -> None:
    """Delete a user and all their data (GDPR right to erasure)."""
    import psycopg

    if not args.confirm:
        print(
            f"This will permanently delete user '{args.user_id}' and ALL their data.",
            file=sys.stderr,
        )
        print("Re-run with --confirm to proceed.", file=sys.stderr)
        sys.exit(1)

    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        # Delete cascading data first (entries cascade to embeddings)
        cur.execute("DELETE FROM reads WHERE owner_id = %s", (args.user_id,))
        reads_deleted = cur.rowcount
        cur.execute("DELETE FROM actions WHERE owner_id = %s", (args.user_id,))
        actions_deleted = cur.rowcount
        cur.execute("DELETE FROM entries WHERE owner_id = %s", (args.user_id,))
        entries_deleted = cur.rowcount
        cur.execute("DELETE FROM users WHERE id = %s", (args.user_id,))
        user_deleted = cur.rowcount

    if user_deleted == 0:
        print(f"Error: user '{args.user_id}' not found", file=sys.stderr)
        sys.exit(1)
    print(
        f"Deleted user '{args.user_id}': "
        f"{entries_deleted} entries, {reads_deleted} reads, {actions_deleted} actions."
    )


def user_main() -> None:
    """User management CLI."""
    parser = argparse.ArgumentParser(description="Manage mcp-awareness users")
    sub = parser.add_subparsers(dest="command", required=True)

    # add
    add_p = sub.add_parser("add", help="Add a user")
    add_p.add_argument("user_id", help="User ID")
    add_p.add_argument("--email", help="Email address")
    add_p.add_argument("--display-name", help="Display name")
    add_p.add_argument("--phone", help="Phone (E.164 format)")
    add_p.add_argument("--timezone", default="UTC", help="IANA timezone")

    # list
    sub.add_parser("list", help="List users")

    # set-password
    sp = sub.add_parser("set-password", help="Set user password")
    sp.add_argument("user_id", help="User ID")

    # export
    exp = sub.add_parser("export", help="Export user data (GDPR)")
    exp.add_argument("user_id", help="User ID")
    exp.add_argument("--output", "-o", default="-", help="Output file (- for stdout)")

    # delete
    dl = sub.add_parser("delete", help="Delete user and all data (GDPR)")
    dl.add_argument("user_id", help="User ID")
    dl.add_argument("--confirm", action="store_true", help="Confirm deletion")

    args = parser.parse_args()

    dsn = _get_dsn()

    if args.command == "add":
        _user_add(dsn, args)
    elif args.command == "list":
        _user_list(dsn)
    elif args.command == "set-password":
        _user_set_password(dsn, args)
    elif args.command == "export":
        _user_export(dsn, args)
    elif args.command == "delete":
        _user_delete(dsn, args)
