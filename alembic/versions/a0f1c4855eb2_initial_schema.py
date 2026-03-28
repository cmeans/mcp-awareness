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

"""initial schema

Revision ID: a0f1c4855eb2
Revises:
Create Date: 2026-03-21 20:11:43.278945

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a0f1c4855eb2"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


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
