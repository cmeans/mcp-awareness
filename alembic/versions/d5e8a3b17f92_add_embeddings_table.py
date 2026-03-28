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

"""add embeddings table for semantic search

Revision ID: d5e8a3b17f92
Revises: c3a7f2d91e4b
Create Date: 2026-03-23 22:00:00.000000

"""

import os
from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d5e8a3b17f92"
down_revision: str | Sequence[str] | None = "c3a7f2d91e4b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


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
