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

import logging
from collections.abc import Sequence

from alembic import op

logger = logging.getLogger("alembic.runtime.migration")

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
    """Remove the _system user, if safe to do so.

    This downgrade is a no-op when `_system`-owned entries still exist (schemas
    seeded via ``mcp-awareness-register-schema --system``, for example). A hard
    DELETE would FK-fail and abort the entire downgrade transaction — masking
    any subsequent downgrade steps from surfacing. The warning surfaces the
    manual step required: operators who really want to remove `_system` must
    first soft-delete or re-home the referenced entries, then re-run downgrade.
    """
    conn = op.get_bind()
    referenced = conn.exec_driver_sql(
        "SELECT 1 FROM entries WHERE owner_id = '_system' LIMIT 1"
    ).fetchone()
    if referenced is not None:
        logger.warning(
            "Skipping delete of users._system — entries still reference it. "
            "Soft-delete or re-home those entries, then re-run downgrade."
        )
        return
    op.execute("DELETE FROM users WHERE id = '_system'")
