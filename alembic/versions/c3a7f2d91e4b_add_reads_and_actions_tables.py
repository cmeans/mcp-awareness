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

"""add reads and actions tables

Revision ID: c3a7f2d91e4b
Revises: 9184f91831f8
Create Date: 2026-03-23 17:00:00.000000

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c3a7f2d91e4b"
down_revision: str | Sequence[str] | None = "9184f91831f8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


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
