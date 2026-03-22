"""add pgvector extension

Revision ID: 9184f91831f8
Revises: a0f1c4855eb2
Create Date: 2026-03-21 20:15:15.737637

"""

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "9184f91831f8"
down_revision: Union[str, Sequence[str], None] = "a0f1c4855eb2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Enable pgvector extension for future embedding/RAG support."""
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")


def downgrade() -> None:
    """Remove pgvector extension."""
    op.execute("DROP EXTENSION IF EXISTS vector")
