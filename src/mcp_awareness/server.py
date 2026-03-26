"""FastMCP server — resources + tools for the awareness service.

Transport is selected via the AWARENESS_TRANSPORT environment variable:
  - "stdio" (default): stdin/stdout, for direct MCP client integration
  - "streamable-http": HTTP server on AWARENESS_HOST:AWARENESS_PORT/mcp

All mutable state (store, mcp, _embedding_provider, env-derived constants)
lives here so that test monkeypatching via ``server_mod.X = …`` works
through a single module.
"""

from __future__ import annotations

import concurrent.futures
import logging
import os
import pathlib
import re
import time
from datetime import datetime, timezone
from typing import Any, Literal

from mcp.server.fastmcp import FastMCP

from .embeddings import (
    EmbeddingProvider,
    compose_embedding_text,
    create_provider,
    should_embed,
    text_hash,
)
from .helpers import (  # noqa: F401 — re-exports for backward compat
    DEFAULT_QUERY_LIMIT,
    VALID_ALERT_LEVELS,
    VALID_ALERT_TYPES,
    VALID_URGENCY,
    _log_timing,
    _parse_entry_type,
    _timed,
    _validate_pagination,
)
from .postgres_store import PostgresStore
from .schema import Entry, EntryType, now_utc
from .store import Store

logger = logging.getLogger(__name__)

_start_time = time.monotonic()

# ---------------------------------------------------------------------------
# Environment configuration
# ---------------------------------------------------------------------------

TRANSPORT: Literal["stdio", "streamable-http"] = os.environ.get(  # type: ignore[assignment]
    "AWARENESS_TRANSPORT", "stdio"
)
HOST = os.environ.get("AWARENESS_HOST", "0.0.0.0")
PORT = int(os.environ.get("AWARENESS_PORT", "8420"))
MOUNT_PATH = os.environ.get("AWARENESS_MOUNT_PATH", "")
DATABASE_URL = os.environ.get("AWARENESS_DATABASE_URL", "")

# Embedding provider — optional, configured via env vars
EMBEDDING_PROVIDER = os.environ.get("AWARENESS_EMBEDDING_PROVIDER", "")
EMBEDDING_MODEL = os.environ.get("AWARENESS_EMBEDDING_MODEL", "nomic-embed-text")
OLLAMA_URL = os.environ.get("AWARENESS_OLLAMA_URL", "http://ollama:11434")
EMBEDDING_DIMENSIONS = int(os.environ.get("AWARENESS_EMBEDDING_DIMENSIONS", "768"))

# ---------------------------------------------------------------------------
# Store (lazy-initialised)
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
    return PostgresStore(url, embedding_dimensions=EMBEDDING_DIMENSIONS)


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
# Embedding helpers
# ---------------------------------------------------------------------------

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
    """Actual embedding work — runs in thread pool.

    Uses the store's connection pool for the DB write.
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
            store.upsert_embedding(
                entry_id, provider.model_name, provider.dimensions, h, vectors[0]
            )
    except Exception:
        logger.debug("Embedding failed for entry %s", entry_id, exc_info=True)


def _generate_embedding(entry: Entry) -> None:
    """Submit embedding generation to background thread pool. Never blocks."""
    if not should_embed(entry):
        return
    entry_type_val = entry.type.value if isinstance(entry.type, EntryType) else entry.type
    _embedding_pool.submit(
        _do_embed, entry.id, entry.source, list(entry.tags), dict(entry.data), entry_type_val
    )


def _log_reads(entries: list[Any], tool_name: str) -> None:
    """Log that entries were read. Fire-and-forget — never blocks the response."""
    try:
        ids = [e.id for e in entries if hasattr(e, "id")]
        if ids:
            store.log_read(ids, tool_used=tool_name)
    except Exception:
        logger.debug("Read logging failed for %s", tool_name, exc_info=True)


# ---------------------------------------------------------------------------
# FastMCP instance
# ---------------------------------------------------------------------------

_INSTRUCTIONS_PATH = pathlib.Path(__file__).parent / "instructions.md"
_INSTRUCTIONS = _INSTRUCTIONS_PATH.read_text(encoding="utf-8").strip()

mcp = FastMCP(
    name="mcp-awareness",
    host=HOST,
    port=PORT,
    instructions=_INSTRUCTIONS,
)

# ---------------------------------------------------------------------------
# User-defined prompts (stored as entries with source="custom-prompt")
# ---------------------------------------------------------------------------

_TEMPLATE_VAR_RE = re.compile(r"\{\{(\w+)\}\}")


def _sync_custom_prompts() -> None:
    """Sync user-defined prompts from the store into the FastMCP registry.

    Each entry with source="custom-prompt" becomes an MCP prompt:
    - logical_key -> prompt name (prefixed with "user/")
    - description -> prompt description
    - content -> template body ({{var}} placeholders become arguments)
    """
    from mcp.server.fastmcp.prompts import Prompt
    from mcp.server.fastmcp.prompts.base import PromptArgument

    entries = store.get_entries(source="custom-prompt")
    # Access _prompts dict for deletion only — no public remove API exists in FastMCP.
    # add_prompt() is used for insertion (public API).
    prompts_dict = mcp._prompt_manager._prompts
    to_remove = [name for name in prompts_dict if name.startswith("user/")]
    for name in to_remove:
        del prompts_dict[name]

    for entry in entries:
        key = entry.logical_key or entry.id
        name = f"user/{key}"
        desc = entry.data.get("description", "")
        template = entry.data.get("content", desc)

        # Extract {{var}} placeholders as prompt arguments
        var_names = _TEMPLATE_VAR_RE.findall(template)
        arguments = [
            PromptArgument(name=v, description=f"Value for {v}", required=True)
            for v in dict.fromkeys(var_names)  # deduplicate, preserve order
        ]

        def _make_fn(tmpl: str) -> Any:
            """Create a closure that renders the template."""

            async def _render(**kwargs: str) -> str:
                result = tmpl
                for k, v in kwargs.items():
                    result = result.replace(f"{{{{{k}}}}}", v)
                return result

            return _render

        prompt = Prompt(
            name=name,
            title=None,
            description=desc,
            arguments=arguments if arguments else None,
            fn=_make_fn(template),
            context_kwarg=None,
        )
        # Force overwrite — add_prompt() skips duplicates, but we need
        # to replace prompts whose content changed in the store.
        prompts_dict[name] = prompt


# Custom prompt sync happens at server start (in main()), not at import time.
# This avoids triggering a DB connection when the module is imported for testing.


def _health_response() -> dict[str, Any]:
    """Build the health check response payload."""
    return {
        "status": "ok",
        "uptime_sec": round(time.monotonic() - _start_time, 1),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "transport": TRANSPORT,
    }


def main() -> None:
    # Sync custom prompts from the store at server start (not at import time)
    _sync_custom_prompts()
    try:
        _run()
    except KeyboardInterrupt:
        print("Shutdown requested — exiting.", flush=True)


def _run() -> None:
    if TRANSPORT == "streamable-http" and MOUNT_PATH:
        import uvicorn

        from mcp_awareness.middleware import SecretPathMiddleware

        inner_app = mcp.streamable_http_app()
        app = SecretPathMiddleware(inner_app, MOUNT_PATH, _health_response)

        config = uvicorn.Config(app, host=HOST, port=PORT)
        server = uvicorn.Server(config)

        import anyio

        anyio.run(server.serve)
    elif TRANSPORT == "streamable-http":
        import uvicorn

        from mcp_awareness.middleware import HealthMiddleware

        inner_app = mcp.streamable_http_app()
        health_app = HealthMiddleware(inner_app, _health_response)

        config = uvicorn.Config(health_app, host=HOST, port=PORT)
        server = uvicorn.Server(config)

        import anyio

        anyio.run(server.serve)
    else:
        mcp.run(transport=TRANSPORT)


# ---------------------------------------------------------------------------
# Import submodules AFTER mcp/store are defined so that @mcp.resource /
# @mcp.tool / @mcp.prompt decorators bind to the live instance at import time.
# Re-export all public names so ``server_mod.get_briefing()`` etc. still work.
# ---------------------------------------------------------------------------

from . import prompts as prompts  # noqa: E402
from . import resources as resources  # noqa: E402
from . import tools as tools  # noqa: E402
from .prompts import (  # noqa: E402, F401
    _extract_entry_number,
    agent_instructions,
    catchup,
    project_context,
    system_status,
    write_guide,
)
from .resources import (  # noqa: E402, F401
    alerts_resource,
    briefing_resource,
    knowledge_resource,
    source_alerts_resource,
    source_status_resource,
    suppressions_resource,
)
from .tools import (  # noqa: E402, F401
    acted_on,
    add_context,
    backfill_embeddings,
    delete_entry,
    get_actions,
    get_activity,
    get_alerts,
    get_briefing,
    get_deleted,
    get_intentions,
    get_knowledge,
    get_reads,
    get_related,
    get_stats,
    get_status,
    get_suppressions,
    get_tags,
    get_unread,
    learn_pattern,
    remember,
    remind,
    report_alert,
    report_status,
    restore_entry,
    semantic_search,
    set_preference,
    suppress_alert,
    update_entry,
    update_intention,
)

if __name__ == "__main__":
    main()
