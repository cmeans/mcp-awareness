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

"""backfill language column on existing entries via lingua detection

Revision ID: l7g8h9i0j1k2
Revises: k6f7g8h9i0j1
Create Date: 2026-04-12 12:00:00.000000

"""

from __future__ import annotations

import logging
from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "l7g8h9i0j1k2"
down_revision: str | Sequence[str] | None = "k6f7g8h9i0j1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

logger = logging.getLogger(__name__)

BATCH_SIZE = 100


def _compose_text(row: dict) -> str:
    """Build detection text from entry fields, mirroring compose_embedding_text logic."""
    import json

    parts: list[str] = []
    data = row["data"]
    if isinstance(data, str):
        data = json.loads(data)
    if desc := data.get("description"):
        parts.append(str(desc))
    if content := data.get("content"):
        parts.append(str(content))
    if goal := data.get("goal"):
        parts.append(str(goal))
    if effect := data.get("effect"):
        parts.append(str(effect))
    if message := data.get("message"):
        parts.append(str(message))
    return " ".join(parts)


def upgrade() -> None:
    """Detect language on existing entries and update the language column.

    Processes entries in batches. Only updates entries where lingua detects
    a language that maps to a known regconfig (i.e., not 'simple'). Entries
    that are already non-simple or where detection returns 'simple' are skipped.
    Idempotent — safe to re-run.
    """
    try:
        from mcp_awareness.language import resolve_language
    except ImportError:
        logger.warning(
            "lingua-language-detector not installed; skipping language backfill. "
            "Install it and re-run 'alembic upgrade head' to backfill."
        )
        return

    conn = op.get_bind()
    sa_text = __import__("sqlalchemy").text
    updated = 0
    while True:
        # Always OFFSET 0: updated rows leave the WHERE clause naturally,
        # so the window advances without an explicit offset.
        rows = (
            conn.execute(
                sa_text(
                    "SELECT id, data FROM entries "
                    "WHERE language = 'simple'::regconfig AND deleted IS NULL "
                    "ORDER BY created "
                    "LIMIT :limit"
                ),
                {"limit": BATCH_SIZE},
            )
            .mappings()
            .all()
        )
        if not rows:
            break
        batch_updated = 0
        for row in rows:
            text = _compose_text(row)
            lang = resolve_language(text_for_detection=text)
            if lang != "simple":
                conn.execute(
                    sa_text("UPDATE entries SET language = :lang::regconfig WHERE id = :id"),
                    {"lang": lang, "id": row["id"]},
                )
                batch_updated += 1
        updated += batch_updated
        # If nothing in this batch was updated, all remaining entries
        # genuinely detect as 'simple' — stop to avoid infinite loop.
        if batch_updated == 0:
            break
    if updated:
        logger.info("Language backfill: updated %d entries", updated)


def downgrade() -> None:
    """Reset all entries back to 'simple' language."""
    op.execute("UPDATE entries SET language = 'simple'::regconfig")
