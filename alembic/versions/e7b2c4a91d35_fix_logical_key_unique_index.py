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

"""fix logical_key unique index to exclude soft-deleted entries

Revision ID: e7b2c4a91d35
Revises: d5e8a3b17f92
Create Date: 2026-03-26 08:00:00.000000

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e7b2c4a91d35"
down_revision: str | Sequence[str] | None = "d5e8a3b17f92"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Replace the unique index to exclude soft-deleted entries.
    # This allows re-creating an entry with the same logical_key after
    # the original has been soft-deleted.
    op.execute("DROP INDEX IF EXISTS idx_entries_source_logical_key")
    op.execute(
        "CREATE UNIQUE INDEX idx_entries_source_logical_key "
        "ON entries(source, logical_key) "
        "WHERE logical_key IS NOT NULL AND deleted IS NULL"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_entries_source_logical_key")
    op.execute(
        "CREATE UNIQUE INDEX idx_entries_source_logical_key "
        "ON entries(source, logical_key) "
        "WHERE logical_key IS NOT NULL"
    )
