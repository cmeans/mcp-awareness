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

# Ensure the URL uses a SQLAlchemy-compatible dialect prefix
if database_url.startswith("postgresql://"):
    database_url = database_url.replace("postgresql://", "postgresql+psycopg://", 1)


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
