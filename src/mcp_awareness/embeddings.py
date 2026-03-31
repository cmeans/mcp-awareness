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

"""Embedding provider abstraction for semantic search.

Defines the EmbeddingProvider protocol and implementations:
  - OllamaEmbedding: local Ollama instance (default)
  - NullEmbedding: graceful degradation when no provider configured

Also provides compose_embedding_text() for building the text representation
of an entry that gets embedded.

Hash stability
--------------
``text_hash()`` computes a SHA-256 digest of the string returned by
``compose_embedding_text()``.  The hash is stored alongside each embedding
in the ``embeddings`` table and compared on subsequent writes to decide
whether re-embedding is needed.

**Any change to ``compose_embedding_text()``** — new fields, reordered
fields, different separators — changes the composed text for every entry
and therefore invalidates every stored hash.  The background embedding
worker will detect the mismatch and re-embed affected entries on its next
cycle.

This is intentional: embeddings must always reflect the current composition
logic.  There is no hash versioning scheme; staleness is detected purely
by comparing the stored hash against a freshly computed one.  If you modify
``compose_embedding_text()``, expect a one-time re-embedding wave across
all entries the next time the background worker runs.
"""

from __future__ import annotations

import hashlib
import json
import logging
import urllib.error
import urllib.request
from typing import Protocol, runtime_checkable

from .schema import Entry, EntryType

logger = logging.getLogger(__name__)

# Entry types that should not be embedded (short-lived, not worth searching)
_SKIP_TYPES = {EntryType.SUPPRESSION}


@runtime_checkable
class EmbeddingProvider(Protocol):
    """Contract for embedding providers."""

    @property
    def model_name(self) -> str: ...

    @property
    def dimensions(self) -> int: ...

    def embed(self, texts: list[str]) -> list[list[float]]: ...

    def is_available(self) -> bool: ...


class NullEmbedding:
    """No-op provider used when embeddings are not configured."""

    @property
    def model_name(self) -> str:
        return ""

    @property
    def dimensions(self) -> int:
        return 0

    def embed(self, texts: list[str]) -> list[list[float]]:
        return []

    def is_available(self) -> bool:
        return False


class OllamaEmbedding:
    """Embedding provider backed by a local Ollama instance.

    Calls POST {base_url}/api/embed with the configured model.
    No SDK dependency — uses stdlib urllib.request.
    """

    def __init__(
        self,
        base_url: str = "http://ollama:11434",
        model: str = "nomic-embed-text",
        dimensions: int = 768,
        timeout: float = 30.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._dimensions = dimensions
        self._timeout = timeout

    @property
    def model_name(self) -> str:
        return self._model

    @property
    def dimensions(self) -> int:
        return self._dimensions

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Generate embeddings for a list of texts.

        Returns a list of float vectors, one per input text.
        Raises on network or API errors.
        """
        url = f"{self._base_url}/api/embed"
        payload = json.dumps({"model": self._model, "input": texts}).encode()
        req = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=self._timeout) as resp:
            data = json.loads(resp.read())
        result: list[list[float]] = data["embeddings"]
        if len(result) != len(texts):
            logger.warning(
                "Ollama returned %d embeddings for %d texts — partial response",
                len(result),
                len(texts),
            )
            raise ValueError(
                f"Ollama returned {len(result)} embeddings for {len(texts)} input texts"
            )
        return result

    def is_available(self) -> bool:
        """Check if Ollama is reachable and the model is loaded."""
        try:
            url = f"{self._base_url}/api/tags"
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=5.0) as resp:
                data = json.loads(resp.read())
            models = [m.get("name", "") for m in data.get("models", [])]
            # Ollama model names may include :latest suffix
            return any(m == self._model or m.startswith(f"{self._model}:") for m in models)
        except (urllib.error.URLError, OSError, ValueError, KeyError):
            return False


def compose_embedding_text(entry: Entry) -> str:
    """Build the text representation of an entry for embedding.

    Combines source, tags, and type-specific content fields into a single
    string that captures the semantic meaning of the entry.

    .. warning::

       Changing this function's output format invalidates all stored
       ``text_hash`` values, triggering a mass re-embedding on the next
       background cycle.  See the module docstring for details.
    """
    parts: list[str] = []
    parts.append(f"type: {entry.type.value}")
    parts.append(f"source: {entry.source}")
    if entry.tags:
        parts.append(f"tags: {', '.join(entry.tags)}")

    data = entry.data
    if desc := data.get("description"):
        parts.append(str(desc))
    if goal := data.get("goal"):
        parts.append(str(goal))
    if msg := data.get("message"):
        parts.append(str(msg))
    if effect := data.get("effect"):
        parts.append(f"effect: {effect}")

    # Preference-specific fields
    if key := data.get("key"):
        parts.append(f"key: {key}")
    if (value := data.get("value")) is not None:
        parts.append(f"value: {value}")
    if scope := data.get("scope"):
        parts.append(f"scope: {scope}")

    # Status-specific fields: metrics and inventory
    if (metrics := data.get("metrics")) and isinstance(metrics, dict):
        metric_parts = [f"{k}={v}" for k, v in metrics.items()]
        parts.append(f"metrics: {', '.join(metric_parts)}")
    if inventory := data.get("inventory"):
        if isinstance(inventory, list):
            parts.append(f"inventory: {', '.join(str(i) for i in inventory)}")
        else:
            parts.append(f"inventory: {inventory}")

    # Content field (truncate if very long to keep embeddings focused)
    _max_content_len = 500
    if content := data.get("content"):
        content_str = str(content)
        if len(content_str) > _max_content_len:
            content_str = content_str[:_max_content_len] + "..."
        parts.append(content_str)

    return "\n".join(parts)


def text_hash(text: str) -> str:
    """SHA-256 hash of the text, used to detect stale embeddings.

    The hash is stored in the ``embeddings.text_hash`` column and compared
    against a freshly computed hash on each write.  A mismatch means the
    entry's composed text has changed and the embedding is stale.
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def should_embed(entry: Entry) -> bool:
    """Whether this entry type should be embedded."""
    return entry.type not in _SKIP_TYPES


def create_provider(
    provider: str = "",
    model: str = "nomic-embed-text",
    ollama_url: str = "http://ollama:11434",
    dimensions: int = 768,
) -> EmbeddingProvider:
    """Factory to create the configured embedding provider.

    Returns NullEmbedding if provider is empty or unrecognized.
    """
    if provider == "ollama":
        return OllamaEmbedding(
            base_url=ollama_url,
            model=model,
            dimensions=dimensions,
        )
    if provider:
        logger.warning("Unknown embedding provider %r, falling back to NullEmbedding", provider)
    return NullEmbedding()
