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

"""add _system user for system-owned schemas

Revision ID: m8h9i0j1k2l3
Revises: l7g8h9i0j1k2
Create Date: 2026-04-13 00:00:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "m8h9i0j1k2l3"
down_revision: str | Sequence[str] | None = "l7g8h9i0j1k2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Seed the _system user for system-owned schema entries.

    Idempotent — ON CONFLICT DO NOTHING lets the migration run multiple
    times safely (e.g., after a stamp-and-reapply).
    """
    op.execute(
        "INSERT INTO users (id, display_name) "
        "VALUES ('_system', 'System-managed schemas') "
        "ON CONFLICT (id) DO NOTHING"
    )


def downgrade() -> None:
    """Remove the _system user.

    Will fail if any entries still reference owner_id='_system'. Operators
    must soft-delete or re-home such entries before downgrade.
    """
    op.execute("DELETE FROM users WHERE id = '_system'")
