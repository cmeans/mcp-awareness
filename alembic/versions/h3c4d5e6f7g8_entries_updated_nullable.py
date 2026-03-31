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

"""make entries.updated nullable (NULL on insert, set on update)

Revision ID: h3c4d5e6f7g8
Revises: g2b3c4d5e6f7
Create Date: 2026-03-29 04:00:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "h3c4d5e6f7g8"
down_revision: str | Sequence[str] | None = "g2b3c4d5e6f7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE entries ALTER COLUMN updated DROP NOT NULL")
    # Backfill: entries that were never actually updated have updated == created.
    # Set those to NULL so the column reflects reality.
    #
    # NOTE: This is a one-time backfill operation. For large tables (>100K rows),
    # the single UPDATE may be slow. Consider running in batches instead:
    #
    #   UPDATE entries SET updated = NULL
    #     WHERE id IN (
    #       SELECT id FROM entries WHERE updated = created LIMIT 10000
    #     );
    #   -- repeat until 0 rows affected
    #
    op.execute("UPDATE entries SET updated = NULL WHERE updated = created")


def downgrade() -> None:
    # Restore updated from created for rows where it's NULL, then re-add NOT NULL.
    op.execute("UPDATE entries SET updated = COALESCE(updated, created) WHERE updated IS NULL")
    op.execute("ALTER TABLE entries ALTER COLUMN updated SET NOT NULL")
