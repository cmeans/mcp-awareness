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

"""Alembic environment configuration.

Reads database URL from AWARENESS_DATABASE_URL environment variable.
Supports both online (direct connection) and offline (SQL script) modes.
No SQLAlchemy models — migrations use raw SQL via op.execute().
"""

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool

# Alembic Config object
config = context.config

# Set up logging from alembic.ini
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = None

# Read database URL from environment
database_url = os.environ.get("AWARENESS_DATABASE_URL", "")
if not database_url:
    raise ValueError(
        "AWARENESS_DATABASE_URL environment variable is required. "
        "Example: postgresql+psycopg://awareness:awareness-dev@localhost:5432/awareness"
    )

# Normalise to a SQLAlchemy-compatible URL.  Production deployments often
# use psycopg DSN format (key=value pairs); Alembic/SQLAlchemy needs a URL.
from mcp_awareness.helpers import dsn_to_sqlalchemy_url  # noqa: E402

database_url = dsn_to_sqlalchemy_url(database_url)


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode — emits SQL to stdout."""
    context.configure(
        url=database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode — connects to the database."""
    connectable = create_engine(database_url, poolclass=pool.NullPool)

    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
