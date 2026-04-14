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

"""RLS: allow all owners to read _system-owned schema entries

Revision ID: n9i0j1k2l3m4
Revises: m8h9i0j1k2l3
Create Date: 2026-04-14 00:00:00.000000

The owner_isolation SELECT/UPDATE/DELETE policy on entries was

    USING (owner_id = current_setting('app.current_user', true))

which — under FORCE ROW LEVEL SECURITY for non-superuser roles — filters
out `_system`-owned rows. That blocks the schema-fallback design for
built-in schemas registered via ``mcp-awareness-register-schema --system``
because the `find_schema` query's ``owner_id IN (%s, '_system')`` clause is
evaluated AFTER RLS strips the `_system` row.

This migration narrows the read carve-out to `_system`-owned *schema* rows
only. Writes remain isolated by the existing `owner_insert` WITH CHECK
policy (which still requires `owner_id = current_user`), so operators
cannot accidentally write to `_system` via the MCP path — the CLI
(`mcp-awareness-register-schema --system`) bypasses MCP and connects as
whichever DB role the operator chose.

Rationale: option 1 from the PR #287 Round-2 QA review (narrowest
change, read-only carve-out, no SECURITY DEFINER functions needed).
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "n9i0j1k2l3m4"
down_revision: str | Sequence[str] | None = "m8h9i0j1k2l3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Replace the owner_isolation policy on `entries` to allow reads of
    `_system`-owned schema rows from any owner context."""
    op.execute("DROP POLICY IF EXISTS owner_isolation ON entries")
    op.execute("""
        CREATE POLICY owner_isolation ON entries
            USING (
                owner_id = current_setting('app.current_user', true)
                OR (owner_id = '_system' AND type = 'schema')
            )
    """)


def downgrade() -> None:
    """Restore the strict-isolation policy on `entries`."""
    op.execute("DROP POLICY IF EXISTS owner_isolation ON entries")
    op.execute("""
        CREATE POLICY owner_isolation ON entries
            USING (owner_id = current_setting('app.current_user', true))
    """)
