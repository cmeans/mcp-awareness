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

"""add owner_id to all tables and create users table

Revision ID: f1a2b3c4d5e6
Revises: e7b2c4a91d35
Create Date: 2026-03-28 14:00:00.000000

"""

from __future__ import annotations

import getpass
import os
from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f1a2b3c4d5e6"
down_revision: str | Sequence[str] | None = "e7b2c4a91d35"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Resolve default owner: env var > system username > 'system'
try:
    _fallback_user = getpass.getuser()
except Exception:
    _fallback_user = "system"

DEFAULT_OWNER = os.environ.get("AWARENESS_DEFAULT_OWNER", _fallback_user)
# Escape single quotes for safe SQL interpolation (e.g., O'Brien)
_escaped = DEFAULT_OWNER.replace("'", "''")


def upgrade() -> None:
    # --- 1. Create users table ---
    op.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id              TEXT PRIMARY KEY,
            email           TEXT,
            canonical_email TEXT UNIQUE,
            email_verified  BOOLEAN NOT NULL DEFAULT FALSE,
            phone           TEXT,
            phone_verified  BOOLEAN NOT NULL DEFAULT FALSE,
            password_hash   TEXT,
            display_name    TEXT,
            timezone        TEXT DEFAULT 'UTC',
            preferences     JSONB NOT NULL DEFAULT '{}',
            created         TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated         TIMESTAMPTZ,
            deleted         TIMESTAMPTZ
        )
    """)

    # --- 2. Add owner_id to entries (nullable first, then backfill, then NOT NULL + DEFAULT) ---
    #
    # NOTE: The backfill UPDATEs below are one-time operations that run on all
    # existing rows per table. For small-to-moderate tables this completes in
    # seconds, but on large tables (>100K rows) the single UPDATE can hold a
    # lock for an extended period. If you encounter this on a large deployment,
    # consider running the backfill in batches before applying NOT NULL:
    #
    #   UPDATE entries SET owner_id = '<default>'
    #     WHERE id IN (
    #       SELECT id FROM entries WHERE owner_id IS NULL LIMIT 10000
    #     );
    #   -- repeat until 0 rows affected, then ALTER ... SET NOT NULL
    #
    op.execute("ALTER TABLE entries ADD COLUMN IF NOT EXISTS owner_id TEXT")
    op.execute(f"UPDATE entries SET owner_id = '{_escaped}' WHERE owner_id IS NULL")
    op.execute("ALTER TABLE entries ALTER COLUMN owner_id SET NOT NULL")
    op.execute(f"ALTER TABLE entries ALTER COLUMN owner_id SET DEFAULT '{_escaped}'")

    # --- 3. Add owner_id to reads (same batching note as above applies) ---
    op.execute("ALTER TABLE reads ADD COLUMN IF NOT EXISTS owner_id TEXT")
    op.execute(f"UPDATE reads SET owner_id = '{_escaped}' WHERE owner_id IS NULL")
    op.execute("ALTER TABLE reads ALTER COLUMN owner_id SET NOT NULL")
    op.execute(f"ALTER TABLE reads ALTER COLUMN owner_id SET DEFAULT '{_escaped}'")

    # --- 4. Add owner_id to actions (same batching note as above applies) ---
    op.execute("ALTER TABLE actions ADD COLUMN IF NOT EXISTS owner_id TEXT")
    op.execute(f"UPDATE actions SET owner_id = '{_escaped}' WHERE owner_id IS NULL")
    op.execute("ALTER TABLE actions ALTER COLUMN owner_id SET NOT NULL")
    op.execute(f"ALTER TABLE actions ALTER COLUMN owner_id SET DEFAULT '{_escaped}'")

    # --- 5. Add owner_id to embeddings (same batching note as above applies) ---
    op.execute("ALTER TABLE embeddings ADD COLUMN IF NOT EXISTS owner_id TEXT")
    op.execute(f"UPDATE embeddings SET owner_id = '{_escaped}' WHERE owner_id IS NULL")
    op.execute("ALTER TABLE embeddings ALTER COLUMN owner_id SET NOT NULL")
    op.execute(f"ALTER TABLE embeddings ALTER COLUMN owner_id SET DEFAULT '{_escaped}'")

    # --- 6. Insert default user ---
    op.execute(f"""
        INSERT INTO users (id) VALUES ('{_escaped}')
        ON CONFLICT (id) DO NOTHING
    """)

    # --- 7. Update unique index on logical_key to include owner_id ---
    op.execute("DROP INDEX IF EXISTS idx_entries_source_logical_key")
    op.execute("""
        CREATE UNIQUE INDEX idx_entries_source_logical_key
            ON entries(owner_id, source, logical_key)
            WHERE logical_key IS NOT NULL AND deleted IS NULL
    """)

    # --- 8. Add owner_id indexes ---
    # Entries: replace single-column indexes with owner-prefixed versions
    op.execute("DROP INDEX IF EXISTS idx_entries_type")
    op.execute("DROP INDEX IF EXISTS idx_entries_source")
    op.execute("DROP INDEX IF EXISTS idx_entries_type_source")
    op.execute("CREATE INDEX idx_entries_owner ON entries(owner_id)")
    op.execute("CREATE INDEX idx_entries_owner_type ON entries(owner_id, type)")
    op.execute("CREATE INDEX idx_entries_owner_source ON entries(owner_id, source)")
    op.execute(
        "CREATE INDEX idx_entries_owner_type_source ON entries(owner_id, type, source)"
    )

    # Reads, actions, embeddings: add owner_id index
    op.execute("CREATE INDEX idx_reads_owner ON reads(owner_id)")
    op.execute("CREATE INDEX idx_actions_owner ON actions(owner_id)")
    op.execute("CREATE INDEX idx_embeddings_owner ON embeddings(owner_id)")


def downgrade() -> None:
    # Remove owner_id indexes
    op.execute("DROP INDEX IF EXISTS idx_embeddings_owner")
    op.execute("DROP INDEX IF EXISTS idx_actions_owner")
    op.execute("DROP INDEX IF EXISTS idx_reads_owner")
    op.execute("DROP INDEX IF EXISTS idx_entries_owner_type_source")
    op.execute("DROP INDEX IF EXISTS idx_entries_owner_source")
    op.execute("DROP INDEX IF EXISTS idx_entries_owner_type")
    op.execute("DROP INDEX IF EXISTS idx_entries_owner")

    # Restore original single-column indexes
    op.execute("CREATE INDEX IF NOT EXISTS idx_entries_type ON entries(type)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_entries_source ON entries(source)")
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_entries_type_source ON entries(type, source)"
    )

    # Restore original unique index (without owner_id)
    op.execute("DROP INDEX IF EXISTS idx_entries_source_logical_key")
    op.execute("""
        CREATE UNIQUE INDEX idx_entries_source_logical_key
            ON entries(source, logical_key)
            WHERE logical_key IS NOT NULL AND deleted IS NULL
    """)

    # Remove owner_id columns
    op.execute("ALTER TABLE embeddings DROP COLUMN IF EXISTS owner_id")
    op.execute("ALTER TABLE actions DROP COLUMN IF EXISTS owner_id")
    op.execute("ALTER TABLE reads DROP COLUMN IF EXISTS owner_id")
    op.execute("ALTER TABLE entries DROP COLUMN IF EXISTS owner_id")

    # Drop users table
    op.execute("DROP TABLE IF EXISTS users")
