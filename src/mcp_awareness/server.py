"""FastMCP server — entry point that wires together resources, tools, and prompts.

Transport is selected via the AWARENESS_TRANSPORT environment variable:
  - "stdio" (default): stdin/stdout, for direct MCP client integration
  - "streamable-http": HTTP server on AWARENESS_HOST:AWARENESS_PORT/mcp
"""

from __future__ import annotations

import os
import pathlib
import sys
from types import ModuleType
from typing import Any, Literal

from mcp.server.fastmcp import FastMCP

from . import helpers as _helpers_mod
from .helpers import (  # noqa: F401
    _generate_embedding,
    _get_embedding_provider,
    _log_reads,
    _timed,
    _validate_pagination,
)
from .prompts import _sync_custom_prompts as _sync_custom_prompts_impl
from .prompts import agent_instructions as agent_instructions
from .prompts import catchup as catchup
from .prompts import project_context as project_context
from .prompts import register_prompts
from .prompts import system_status as system_status
from .prompts import write_guide as write_guide
from .resources import (  # noqa: F401
    alerts_resource,
    briefing_resource,
    knowledge_resource,
    register_resources,
    source_alerts_resource,
    source_status_resource,
    suppressions_resource,
)
from .tools import (  # noqa: F401
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
    register_tools,
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

# ---------------------------------------------------------------------------
# Shared state forwarding
# ---------------------------------------------------------------------------
# Tests monkeypatch ``server_mod.store`` and ``server_mod._embedding_provider``.
# These live on the ``helpers`` module where all handler code reads them.
# A module __getattr__/__setattr__ pair forwards reads and writes so that
# ``server_mod.store = X`` is equivalent to ``helpers.store = X``.
# ---------------------------------------------------------------------------

_FORWARDED_ATTRS = {"store", "_embedding_provider"}


def __getattr__(name: str) -> Any:
    if name in _FORWARDED_ATTRS:
        return getattr(_helpers_mod, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


# Module-level __setattr__ requires replacing the module in sys.modules
# with a wrapper.  We use a thin subclass that intercepts attribute writes
# for the forwarded names and delegates everything else.

_real_module = sys.modules[__name__]


class _ForwardingModule(ModuleType):
    """Module wrapper that forwards writes of shared mutable state to helpers."""

    def __setattr__(self, name: str, value: Any) -> None:
        if name in _FORWARDED_ATTRS:
            setattr(_helpers_mod, name, value)
        else:
            super().__setattr__(name, value)

    def __getattr__(self, name: str) -> Any:
        if name in _FORWARDED_ATTRS:
            return getattr(_helpers_mod, name)
        return getattr(_real_module, name)


_wrapper = _ForwardingModule(__name__)
_wrapper.__dict__.update({k: v for k, v in _real_module.__dict__.items() if k != "__dict__"})
_wrapper.__spec__ = _real_module.__spec__
sys.modules[__name__] = _wrapper

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

TRANSPORT: Literal["stdio", "streamable-http"] = os.environ.get(  # type: ignore[assignment]
    "AWARENESS_TRANSPORT", "stdio"
)
HOST = os.environ.get("AWARENESS_HOST", "0.0.0.0")
PORT = int(os.environ.get("AWARENESS_PORT", "8420"))
MOUNT_PATH = os.environ.get("AWARENESS_MOUNT_PATH", "")

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
# Register handlers from submodules
# ---------------------------------------------------------------------------

register_resources(mcp, _helpers_mod.store)
register_tools(mcp, _helpers_mod.store)
register_prompts(mcp, _helpers_mod.store)


def _sync_custom_prompts() -> None:
    """Backward-compatible wrapper — tests call this with no arguments."""
    _sync_custom_prompts_impl(mcp, _helpers_mod.store)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    # Sync custom prompts from the store at server start (not at import time)
    _sync_custom_prompts()
    try:
        _run()
    except KeyboardInterrupt:
        print("Shutdown requested — exiting.", flush=True)


def _run() -> None:
    if TRANSPORT == "streamable-http" and MOUNT_PATH:
        import anyio
        import uvicorn

        from .middleware import SecretPathMiddleware

        inner_app = mcp.streamable_http_app()
        app = SecretPathMiddleware(inner_app, MOUNT_PATH, TRANSPORT)

        config = uvicorn.Config(app, host=HOST, port=PORT)
        server = uvicorn.Server(config)
        anyio.run(server.serve)
    elif TRANSPORT == "streamable-http":
        import anyio
        import uvicorn

        from .middleware import HealthMiddleware

        inner_app = mcp.streamable_http_app()
        health_app = HealthMiddleware(inner_app, TRANSPORT)

        config = uvicorn.Config(health_app, host=HOST, port=PORT)
        server = uvicorn.Server(config)
        anyio.run(server.serve)
    else:
        mcp.run(transport=TRANSPORT)


if __name__ == "__main__":
    main()
