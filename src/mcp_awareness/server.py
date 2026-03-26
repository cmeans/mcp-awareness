"""FastMCP server — entry point that wires together resources, tools, and prompts.

Transport is selected via the AWARENESS_TRANSPORT environment variable:
  - "stdio" (default): stdin/stdout, for direct MCP client integration
  - "streamable-http": HTTP server on AWARENESS_HOST:AWARENESS_PORT/mcp
"""

from __future__ import annotations

import os
import pathlib
from typing import Literal

from mcp.server.fastmcp import FastMCP

# Re-export shared state and helpers so tests (and external code) that import
# from `mcp_awareness.server` continue to work without changes.
from .helpers import (  # noqa: F401
    _generate_embedding,
    _get_embedding_provider,
    _log_reads,
    _timed,
    _validate_pagination,
    store,
)
from .prompts import (  # noqa: F401
    _sync_custom_prompts,
    agent_instructions,
    catchup,
    project_context,
    register_prompts,
    system_status,
    write_guide,
)
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

register_resources(mcp, store)
register_tools(mcp, store)
register_prompts(mcp, store)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    # Sync custom prompts from the store at server start (not at import time)
    _sync_custom_prompts(mcp, store)
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
