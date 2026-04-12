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

"""add language column and tsv generated tsvector for hybrid FTS retrieval

Revision ID: k6f7g8h9i0j1
Revises: j5e6f7g8h9i0
Create Date: 2026-04-12 00:00:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "k6f7g8h9i0j1"
down_revision: str | Sequence[str] | None = "j5e6f7g8h9i0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add language regconfig column and generated tsvector column with indexes."""
    # Language column — regconfig type, defaults to 'simple' (word-boundary only)
    op.execute(
        "ALTER TABLE entries ADD COLUMN language regconfig NOT NULL DEFAULT 'simple'"
    )

    # Generated tsvector column with weighted fields:
    #   A = description (most distinctive), B = content/goal, C = tags
    op.execute("""
        ALTER TABLE entries ADD COLUMN tsv tsvector GENERATED ALWAYS AS (
            setweight(to_tsvector(language, coalesce(data->>'description', '')), 'A') ||
            setweight(to_tsvector(language, coalesce(data->>'content', '')), 'B') ||
            setweight(to_tsvector(language, coalesce(data->>'goal', '')), 'B') ||
            setweight(to_tsvector(language, coalesce(translate(tags::text, '[]"', '   '), '')), 'C')
        ) STORED
    """)

    # GIN index for FTS queries (e.tsv @@ query)
    op.execute("CREATE INDEX idx_entries_tsv ON entries USING GIN (tsv)")

    # Partial index on language for filtering non-simple entries
    op.execute(
        "CREATE INDEX idx_entries_language ON entries(language) "
        "WHERE language != 'simple'::regconfig"
    )


def downgrade() -> None:
    """Remove FTS infrastructure."""
    op.execute("DROP INDEX IF EXISTS idx_entries_language")
    op.execute("DROP INDEX IF EXISTS idx_entries_tsv")
    op.execute("ALTER TABLE entries DROP COLUMN IF EXISTS tsv")
    op.execute("ALTER TABLE entries DROP COLUMN IF EXISTS language")
