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

"""add OAuth identity columns to users table

Revision ID: i4d5e6f7g8h9
Revises: h3c4d5e6f7g8
Create Date: 2026-03-29 20:00:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "i4d5e6f7g8h9"
down_revision: str | Sequence[str] | None = "h3c4d5e6f7g8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # OAuth identity: sub claim + issuer for provider-agnostic lookup
    op.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS oauth_subject TEXT")
    op.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS oauth_issuer TEXT")
    # Unique constraint: one OAuth identity per provider per user
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ix_users_oauth_identity "
        "ON users (oauth_issuer, oauth_subject) WHERE oauth_issuer IS NOT NULL"
    )
    # Fast lookup index for every authenticated request
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_users_oauth_subject "
        "ON users (oauth_subject) WHERE oauth_subject IS NOT NULL"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_users_oauth_subject")
    op.execute("DROP INDEX IF EXISTS ix_users_oauth_identity")
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS oauth_issuer")
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS oauth_subject")
