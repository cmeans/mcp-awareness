"""Tests for the embedding provider abstraction and text composition."""

from __future__ import annotations

from mcp_awareness.embeddings import (
    NullEmbedding,
    compose_embedding_text,
    create_provider,
    should_embed,
    text_hash,
)
from mcp_awareness.schema import Entry, EntryType, now_utc


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
        from mcp_awareness.embeddings import OllamaEmbedding

        p = create_provider(provider="ollama", ollama_url="http://localhost:11434")
        assert isinstance(p, OllamaEmbedding)
        assert p.model_name == "nomic-embed-text"
        assert p.dimensions == 768
