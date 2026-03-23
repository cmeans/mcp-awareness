"""add reads and actions tables

Revision ID: c3a7f2d91e4b
Revises: 9184f91831f8
Create Date: 2026-03-23 17:00:00.000000

"""

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c3a7f2d91e4b"
down_revision: Union[str, Sequence[str], None] = "9184f91831f8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create reads and actions tables for read/action tracking."""
    op.execute("""
        CREATE TABLE IF NOT EXISTS reads (
            id       SERIAL PRIMARY KEY,
            entry_id TEXT NOT NULL REFERENCES entries(id) ON DELETE CASCADE,
            timestamp TIMESTAMPTZ NOT NULL DEFAULT now(),
            platform TEXT,
            tool_used TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_reads_entry ON reads(entry_id);
        CREATE INDEX IF NOT EXISTS idx_reads_timestamp ON reads(timestamp);

        CREATE TABLE IF NOT EXISTS actions (
            id       SERIAL PRIMARY KEY,
            entry_id TEXT NOT NULL REFERENCES entries(id) ON DELETE CASCADE,
            timestamp TIMESTAMPTZ NOT NULL DEFAULT now(),
            platform TEXT,
            action   TEXT NOT NULL,
            detail   TEXT,
            tags     JSONB NOT NULL DEFAULT '[]'
        );
        CREATE INDEX IF NOT EXISTS idx_actions_entry ON actions(entry_id);
        CREATE INDEX IF NOT EXISTS idx_actions_timestamp ON actions(timestamp);
        CREATE INDEX IF NOT EXISTS idx_actions_tags_gin ON actions USING GIN (tags);
    """)


def downgrade() -> None:
    """Drop reads and actions tables."""
    op.execute("DROP TABLE IF EXISTS actions")
    op.execute("DROP TABLE IF EXISTS reads")
