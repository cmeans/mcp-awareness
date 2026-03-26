"""fix logical_key unique index to exclude soft-deleted entries

Revision ID: e7b2c4a91d35
Revises: d5e8a3b17f92
Create Date: 2026-03-26 08:00:00.000000

"""

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e7b2c4a91d35"
down_revision: Union[str, Sequence[str], None] = "d5e8a3b17f92"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


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
