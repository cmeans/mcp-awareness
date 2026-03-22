"""initial schema

Revision ID: a0f1c4855eb2
Revises:
Create Date: 2026-03-21 20:11:43.278945

"""

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a0f1c4855eb2"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create the entries table with all columns through v0.3.1."""
    op.execute("""
        CREATE TABLE IF NOT EXISTS entries (
            id       TEXT PRIMARY KEY,
            type     TEXT NOT NULL,
            source   TEXT NOT NULL,
            created  TIMESTAMPTZ NOT NULL,
            updated  TIMESTAMPTZ NOT NULL,
            expires  TIMESTAMPTZ,
            deleted  TIMESTAMPTZ,
            tags     JSONB NOT NULL DEFAULT '[]',
            data     JSONB NOT NULL DEFAULT '{}',
            logical_key TEXT
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS idx_entries_type ON entries(type)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_entries_source ON entries(source)")
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_entries_type_source ON entries(type, source)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_entries_tags_gin ON entries USING GIN (tags)"
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_entries_source_logical_key "
        "ON entries(source, logical_key) WHERE logical_key IS NOT NULL"
    )


def downgrade() -> None:
    """Drop the entries table."""
    op.execute("DROP TABLE IF EXISTS entries")
