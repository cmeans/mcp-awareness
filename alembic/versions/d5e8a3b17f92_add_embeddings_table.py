"""add embeddings table for semantic search

Revision ID: d5e8a3b17f92
Revises: c3a7f2d91e4b
Create Date: 2026-03-23 22:00:00.000000

"""

import os
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d5e8a3b17f92"
down_revision: Union[str, Sequence[str], None] = "c3a7f2d91e4b"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create embeddings table with HNSW vector index."""
    dim = os.environ.get("AWARENESS_EMBEDDING_DIMENSIONS", "768")
    op.execute(f"""
        CREATE TABLE IF NOT EXISTS embeddings (
            id          SERIAL PRIMARY KEY,
            entry_id    TEXT NOT NULL REFERENCES entries(id) ON DELETE CASCADE,
            model       TEXT NOT NULL,
            dimensions  INTEGER NOT NULL,
            text_hash   TEXT NOT NULL,
            embedding   VECTOR({dim}) NOT NULL,
            created     TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE (entry_id, model)
        );
        CREATE INDEX IF NOT EXISTS idx_embeddings_entry ON embeddings(entry_id);
        CREATE INDEX IF NOT EXISTS idx_embeddings_vector_hnsw ON embeddings
            USING hnsw (embedding vector_cosine_ops)
            WITH (m = 16, ef_construction = 64);
    """)


def downgrade() -> None:
    """Drop embeddings table."""
    op.execute("DROP TABLE IF EXISTS embeddings")
