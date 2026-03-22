"""Run Alembic migrations for the Postgres backend.

Usage:
    mcp-awareness-migrate              # upgrade to latest
    mcp-awareness-migrate --stamp      # stamp existing DB as current (first time)
    mcp-awareness-migrate --current    # show current migration version
    mcp-awareness-migrate --history    # show migration history

Requires AWARENESS_DATABASE_URL environment variable.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description="Run database migrations")
    parser.add_argument("--stamp", action="store_true", help="Stamp DB as current (no migration)")
    parser.add_argument("--current", action="store_true", help="Show current version")
    parser.add_argument("--history", action="store_true", help="Show migration history")
    parser.add_argument(
        "--downgrade", type=str, default=None, help="Downgrade to specific revision"
    )
    args = parser.parse_args()

    database_url = os.environ.get("AWARENESS_DATABASE_URL", "")
    if not database_url:
        print("Error: AWARENESS_DATABASE_URL is required.", file=sys.stderr)
        print(
            "Example: AWARENESS_DATABASE_URL=postgresql://user:pass@localhost:5432/awareness",
            file=sys.stderr,
        )
        sys.exit(1)

    # Find alembic.ini relative to the package
    alembic_ini = Path(__file__).parent.parent.parent / "alembic.ini"
    if not alembic_ini.exists():
        # Installed package — look in working directory
        alembic_ini = Path("alembic.ini")
    if not alembic_ini.exists():
        print("Error: alembic.ini not found.", file=sys.stderr)
        sys.exit(1)

    from alembic import command
    from alembic.config import Config

    alembic_cfg = Config(str(alembic_ini))

    if args.current:
        command.current(alembic_cfg)
    elif args.history:
        command.history(alembic_cfg)
    elif args.stamp:
        command.stamp(alembic_cfg, "head")
        print("Database stamped as current.")
    elif args.downgrade:
        command.downgrade(alembic_cfg, args.downgrade)
    else:
        command.upgrade(alembic_cfg, "head")
        print("Migrations complete.")


if __name__ == "__main__":
    main()
