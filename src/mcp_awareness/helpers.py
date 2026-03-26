"""Shared helpers, state, and constants used across server modules.

This module owns the lazy store, embedding infrastructure, timing decorator,
pagination validation, and read/action logging.  Imported by tools.py,
resources.py, prompts.py, and server.py — never imports from them.
"""

from __future__ import annotations

import concurrent.futures
import functools
import os
import time
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from .embeddings import (
    EmbeddingProvider,
    compose_embedding_text,
    create_provider,
    should_embed,
    text_hash,
)
from .postgres_store import PostgresStore
from .schema import Entry, EntryType, now_utc
from .store import Store

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_start_time = time.monotonic()

VALID_ALERT_LEVELS = {"warning", "critical"}
VALID_ALERT_TYPES = {"threshold", "structural", "baseline"}
VALID_URGENCY = {"low", "normal", "high"}


# ---------------------------------------------------------------------------
# Lazy store
# ---------------------------------------------------------------------------


def _create_store() -> Store:
    """Create the PostgreSQL storage backend.

    Returns a PostgresStore if DATABASE_URL is set, otherwise raises.
    Called lazily at first use (not at import time) to avoid side effects
    during testing and to allow monkeypatching before initialization.
    """
    url = os.environ.get("AWARENESS_DATABASE_URL", "")
    if not url:
        raise ValueError(
            "AWARENESS_DATABASE_URL is required. "
            "Example: postgresql://user:pass@localhost:5432/awareness"
        )
    return PostgresStore(url)


class _LazyStore:
    """Descriptor that initializes the store on first attribute access.

    Avoids import-time side effects (DB connections, env var requirements).
    Tests can monkeypatch server_mod.store before any access occurs.
    """

    _instance: Store | None = None

    def __getattr__(self, name: str) -> Any:
        if _LazyStore._instance is None:
            _LazyStore._instance = _create_store()
        return getattr(_LazyStore._instance, name)


store: Any = _LazyStore()


# ---------------------------------------------------------------------------
# Embedding infrastructure
# ---------------------------------------------------------------------------

EMBEDDING_PROVIDER = os.environ.get("AWARENESS_EMBEDDING_PROVIDER", "")
EMBEDDING_MODEL = os.environ.get("AWARENESS_EMBEDDING_MODEL", "nomic-embed-text")
OLLAMA_URL = os.environ.get("AWARENESS_OLLAMA_URL", "http://ollama:11434")
EMBEDDING_DIMENSIONS = int(os.environ.get("AWARENESS_EMBEDDING_DIMENSIONS", "768"))

_embedding_provider: EmbeddingProvider | None = None


def _get_embedding_provider() -> EmbeddingProvider:
    """Lazy-init the embedding provider from env vars."""
    global _embedding_provider
    if _embedding_provider is None:
        _embedding_provider = create_provider(
            provider=EMBEDDING_PROVIDER,
            model=EMBEDDING_MODEL,
            ollama_url=OLLAMA_URL,
            dimensions=EMBEDDING_DIMENSIONS,
        )
    return _embedding_provider


# Thread pool for background embedding generation — max 2 workers to avoid
# overwhelming Ollama while keeping writes non-blocking.
_embedding_pool = concurrent.futures.ThreadPoolExecutor(max_workers=2, thread_name_prefix="embed")


def _do_embed(
    entry_id: str,
    entry_source: str,
    entry_tags: list[str],
    entry_data: dict[str, Any],
    entry_type_val: str,
) -> None:
    """Actual embedding work — runs in thread pool with its own DB connection.

    Uses a dedicated connection to avoid racing with the main thread's
    shared connection (same pattern as _do_cleanup in PostgresStore).
    """
    try:
        provider = _get_embedding_provider()
        if not provider.is_available():
            return
        entry = Entry(
            id=entry_id,
            type=EntryType(entry_type_val),
            source=entry_source,
            tags=entry_tags,
            created=now_utc(),
            updated=now_utc(),
            expires=None,
            data=entry_data,
        )
        text = compose_embedding_text(entry)
        h = text_hash(text)
        vectors = provider.embed([text])
        if vectors:
            # Use a dedicated connection for the background write to avoid
            # racing with the main thread's shared connection.
            import psycopg

            with psycopg.connect(store.dsn) as conn:
                vector_literal = "[" + ",".join(str(v) for v in vectors[0]) + "]"
                conn.execute(
                    "INSERT INTO embeddings (entry_id, model, dimensions, text_hash, embedding) "
                    "VALUES (%s, %s, %s, %s, %s::vector) "
                    "ON CONFLICT (entry_id, model) DO UPDATE SET "
                    "embedding = EXCLUDED.embedding, text_hash = EXCLUDED.text_hash, "
                    "dimensions = EXCLUDED.dimensions, created = now()",
                    (entry_id, provider.model_name, provider.dimensions, h, vector_literal),
                )
                conn.commit()
    except Exception:
        pass  # Backfill will catch failures


def _generate_embedding(entry: Entry) -> None:
    """Submit embedding generation to background thread pool. Never blocks."""
    if not should_embed(entry):
        return
    entry_type_val = entry.type.value if isinstance(entry.type, EntryType) else entry.type
    _embedding_pool.submit(
        _do_embed, entry.id, entry.source, list(entry.tags), dict(entry.data), entry_type_val
    )


# ---------------------------------------------------------------------------
# Logging helpers
# ---------------------------------------------------------------------------


def _log_reads(entries: list[Any], tool_name: str) -> None:
    """Log that entries were read. Fire-and-forget — never blocks the response."""
    try:
        ids = [e.id for e in entries if hasattr(e, "id")]
        if ids:
            store.log_read(ids, tool_used=tool_name)
    except Exception:
        pass  # Read logging must never break the tool response


def _log_timing(tool_name: str, elapsed_ms: float) -> None:
    """Log tool call timing to stdout (Docker captures automatically)."""
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    print(f"{ts} | {tool_name} | {elapsed_ms:.1f}ms", flush=True)


def _timed(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Decorator that logs wall-clock time for each tool/resource call."""

    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        t0 = time.monotonic()
        result = await fn(*args, **kwargs)
        _log_timing(fn.__name__, (time.monotonic() - t0) * 1000)
        return result

    return wrapper


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


def _validate_pagination(
    limit: int | None, offset: int | None
) -> tuple[int | None, int | None] | str:
    """Validate and clamp pagination params. Returns (limit, offset) or error string."""
    if limit is not None and limit < 0:
        return "limit must be non-negative"
    if offset is not None and offset < 0:
        return "offset must be non-negative"
    return limit, offset
