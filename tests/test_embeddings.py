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

"""Tests for the embedding provider abstraction and text composition."""

from __future__ import annotations

import os

import pytest

from mcp_awareness.embeddings import (
    NullEmbedding,
    OllamaEmbedding,
    compose_embedding_text,
    create_provider,
    should_embed,
    text_hash,
)
from mcp_awareness.schema import Entry, EntryType, now_utc

# Check if Ollama is available (set by CI or local dev)
_OLLAMA_URL = os.environ.get("AWARENESS_OLLAMA_URL", "http://localhost:11434")
_ollama_available: bool | None = None


def _is_ollama_available() -> bool:
    global _ollama_available
    if _ollama_available is None:
        p = OllamaEmbedding(base_url=_OLLAMA_URL)
        _ollama_available = p.is_available()
    return _ollama_available


skip_no_ollama = pytest.mark.skipif(
    "not _is_ollama_available()",
    reason="Ollama not available",
)


def _make_entry(
    entry_type: EntryType = EntryType.NOTE,
    source: str = "test",
    tags: list[str] | None = None,
    data: dict | None = None,
) -> Entry:
    now = now_utc()
    return Entry(
        id="test-id",
        type=entry_type,
        source=source,
        tags=tags or [],
        created=now,
        updated=now,
        expires=None,
        data=data or {},
    )


# ---------------------------------------------------------------------------
# compose_embedding_text
# ---------------------------------------------------------------------------


class TestComposeEmbeddingText:
    def test_note_with_description(self):
        entry = _make_entry(data={"description": "Personal retirement plan"})
        text = compose_embedding_text(entry)
        assert "source: test" in text
        assert "Personal retirement plan" in text

    def test_note_with_content(self):
        entry = _make_entry(
            data={"description": "Config snapshot", "content": "key=value\nfoo=bar"}
        )
        text = compose_embedding_text(entry)
        assert "Config snapshot" in text
        assert "key=value" in text

    def test_alert_uses_message(self):
        entry = _make_entry(
            entry_type=EntryType.ALERT,
            data={"message": "CPU high on NAS", "alert_id": "cpu-1"},
        )
        text = compose_embedding_text(entry)
        assert "CPU high on NAS" in text

    def test_intention_uses_goal(self):
        entry = _make_entry(
            entry_type=EntryType.INTENTION,
            data={"goal": "Buy groceries at Mariano's", "state": "pending"},
        )
        text = compose_embedding_text(entry)
        assert "Buy groceries" in text

    def test_pattern_uses_effect(self):
        entry = _make_entry(
            entry_type=EntryType.PATTERN,
            data={"description": "Maintenance window", "effect": "NAS goes offline Fridays"},
        )
        text = compose_embedding_text(entry)
        assert "effect: NAS goes offline Fridays" in text

    def test_tags_included(self):
        entry = _make_entry(tags=["infra", "nas"], data={"description": "test"})
        text = compose_embedding_text(entry)
        assert "tags: infra, nas" in text

    def test_empty_entry(self):
        entry = _make_entry()
        text = compose_embedding_text(entry)
        assert "source: test" in text

    def test_entry_type_included(self):
        """Entry type should be included to disambiguate note vs context."""
        note = _make_entry(data={"description": "same text"})
        context = _make_entry(entry_type=EntryType.CONTEXT, data={"description": "same text"})
        note_text = compose_embedding_text(note)
        context_text = compose_embedding_text(context)
        assert "type: note" in note_text
        assert "type: context" in context_text
        assert note_text != context_text


# ---------------------------------------------------------------------------
# text_hash
# ---------------------------------------------------------------------------


class TestTextHash:
    def test_deterministic(self):
        assert text_hash("hello") == text_hash("hello")

    def test_changes_on_content_change(self):
        assert text_hash("hello") != text_hash("world")


# ---------------------------------------------------------------------------
# should_embed
# ---------------------------------------------------------------------------


class TestShouldEmbed:
    def test_note(self):
        assert should_embed(_make_entry(EntryType.NOTE)) is True

    def test_pattern(self):
        assert should_embed(_make_entry(EntryType.PATTERN)) is True

    def test_alert(self):
        assert should_embed(_make_entry(EntryType.ALERT)) is True

    def test_intention(self):
        assert should_embed(_make_entry(EntryType.INTENTION)) is True

    def test_suppression_skipped(self):
        assert should_embed(_make_entry(EntryType.SUPPRESSION)) is False


# ---------------------------------------------------------------------------
# NullEmbedding
# ---------------------------------------------------------------------------


class TestNullEmbedding:
    def test_not_available(self):
        p = NullEmbedding()
        assert p.is_available() is False

    def test_embed_returns_empty(self):
        p = NullEmbedding()
        assert p.embed(["hello"]) == []

    def test_model_name_empty(self):
        p = NullEmbedding()
        assert p.model_name == ""

    def test_dimensions_zero(self):
        p = NullEmbedding()
        assert p.dimensions == 0


# ---------------------------------------------------------------------------
# OllamaEmbedding unit tests (no running Ollama needed)
# ---------------------------------------------------------------------------


class TestOllamaEmbeddingUnit:
    def test_unreachable_is_not_available(self):
        """is_available returns False when Ollama is unreachable."""
        p = OllamaEmbedding(base_url="http://localhost:19999")
        assert p.is_available() is False


# ---------------------------------------------------------------------------
# create_provider
# ---------------------------------------------------------------------------


class TestCreateProvider:
    def test_empty_returns_null(self):
        p = create_provider(provider="")
        assert isinstance(p, NullEmbedding)

    def test_unknown_returns_null(self):
        p = create_provider(provider="nonexistent")
        assert isinstance(p, NullEmbedding)

    def test_ollama_creates_provider(self):
        p = create_provider(provider="ollama", ollama_url="http://localhost:11434")
        assert isinstance(p, OllamaEmbedding)
        assert p.model_name == "nomic-embed-text"
        assert p.dimensions == 768


# ---------------------------------------------------------------------------
# OllamaEmbedding integration tests (require running Ollama with model)
# ---------------------------------------------------------------------------


class TestOllamaIntegration:
    @skip_no_ollama
    def test_is_available(self):
        p = OllamaEmbedding(base_url=_OLLAMA_URL)
        assert p.is_available() is True

    @skip_no_ollama
    def test_embed_single_text(self):
        p = OllamaEmbedding(base_url=_OLLAMA_URL)
        vectors = p.embed(["Hello world"])
        assert len(vectors) == 1
        assert len(vectors[0]) == 768
        assert all(isinstance(v, float) for v in vectors[0])

    @skip_no_ollama
    def test_embed_batch(self):
        p = OllamaEmbedding(base_url=_OLLAMA_URL)
        vectors = p.embed(["Hello world", "Goodbye world"])
        assert len(vectors) == 2
        assert len(vectors[0]) == 768
        assert len(vectors[1]) == 768

    @skip_no_ollama
    def test_similar_texts_closer_than_different(self):
        """Semantically similar texts should have higher cosine similarity."""
        p = OllamaEmbedding(base_url=_OLLAMA_URL)
        vectors = p.embed(
            [
                "retirement planning and 401k contributions",
                "pension fund investment strategy",
                "how to fix a leaking kitchen faucet",
            ]
        )

        def cosine_sim(a: list[float], b: list[float]) -> float:
            dot = sum(x * y for x, y in zip(a, b, strict=True))
            norm_a = sum(x * x for x in a) ** 0.5
            norm_b = sum(x * x for x in b) ** 0.5
            return dot / (norm_a * norm_b)

        sim_related = cosine_sim(vectors[0], vectors[1])
        sim_unrelated = cosine_sim(vectors[0], vectors[2])
        assert sim_related > sim_unrelated

    @skip_no_ollama
    def test_is_available_wrong_model(self):
        """is_available returns False when model isn't pulled."""
        p = OllamaEmbedding(base_url=_OLLAMA_URL, model="nonexistent-model-xyz")
        assert p.is_available() is False
