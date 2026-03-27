"""Pure helper functions — no module-level mutable state.

Every function here is either a pure computation or reads its dependencies
from arguments / late imports.  All mutable state (``store``, ``mcp``,
``_embedding_provider``, constants read from env) lives in ``server.py``
so that test monkeypatching works through a single module.
"""

from __future__ import annotations

import functools
import time
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from .schema import EntryType

# Valid values for enum-like parameters
VALID_ALERT_LEVELS = {"warning", "critical"}
VALID_ALERT_TYPES = {"threshold", "structural", "baseline"}
VALID_URGENCY = {"low", "normal", "high"}

DEFAULT_QUERY_LIMIT = 200

_VALID_ENTRY_TYPES = [e.value for e in EntryType]


def _parse_entry_type(entry_type: str | None) -> tuple[EntryType | None, str | None]:
    """Parse entry_type string. Returns (value, None) or (None, error)."""
    if not entry_type:
        return None, None
    try:
        return EntryType(entry_type), None
    except ValueError:
        return None, f"Invalid entry_type: {entry_type!r}. Valid: {_VALID_ENTRY_TYPES}"


def _validate_pagination(
    limit: int | None, offset: int | None, *, default_limit: int = DEFAULT_QUERY_LIMIT
) -> tuple[int | None, int | None] | str:
    """Validate and clamp pagination params. Returns (limit, offset) or error string.

    Applies default_limit when the caller omits limit to prevent unbounded queries.
    """
    if limit is not None and limit < 0:
        return "limit must be non-negative"
    if offset is not None and offset < 0:
        return "offset must be non-negative"
    if limit is None:
        limit = default_limit
    return limit, offset


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
