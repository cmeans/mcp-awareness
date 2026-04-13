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

"""Pure helper functions — no module-level mutable state.

Every function here is either a pure computation or reads its dependencies
from arguments / late imports.  All mutable state (``store``, ``mcp``,
``_embedding_provider``, constants read from env) lives in ``server.py``
so that test monkeypatching works through a single module.
"""

from __future__ import annotations

import functools
import json
import time
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any, NoReturn

from .schema import EntryType

# Valid values for enum-like parameters
VALID_ALERT_LEVELS = {"warning", "critical"}
VALID_ALERT_TYPES = {"threshold", "structural", "baseline"}
VALID_URGENCY = {"low", "normal", "high"}

DEFAULT_QUERY_LIMIT = 100


def dsn_to_sqlalchemy_url(dsn: str) -> str:
    """Convert a database connection string to a SQLAlchemy-compatible URL.

    Accepts either:
    - A psycopg DSN (``host=X dbname=Y user=Z password=W port=P``)
    - A URL (``postgresql://...`` or ``postgresql+psycopg://...``)

    DSN values may be single-quoted (``password='has spaces'``).
    Special characters in user/password are percent-encoded for the URL.

    Always returns a ``postgresql+psycopg://`` URL.
    """
    from urllib.parse import quote

    dsn = dsn.strip()

    # Already a URL — just normalise the dialect prefix
    if dsn.startswith(("postgresql://", "postgresql+psycopg://")):
        if dsn.startswith("postgresql://"):
            dsn = "postgresql+psycopg://" + dsn[len("postgresql://") :]
        return dsn

    # Parse psycopg key=value DSN.  Values may be unquoted or single-quoted.
    import re

    parts: dict[str, str] = {}
    for m in re.finditer(r"(\w+)\s*=\s*(?:'((?:[^'\\]|\\.)*)'|(\S+))", dsn):
        key = m.group(1)
        # group(2) is the quoted value, group(3) the unquoted value
        val = m.group(2) if m.group(2) is not None else m.group(3)
        # Un-escape backslash sequences inside quoted values
        if m.group(2) is not None:
            val = val.replace("\\'", "'").replace("\\\\", "\\")
        parts[key] = val

    host = parts.get("host", "localhost")
    port = parts.get("port", "5432")
    dbname = parts.get("dbname", "awareness")
    user = quote(parts.get("user", "awareness"), safe="")
    password = quote(parts.get("password", ""), safe="")
    return f"postgresql+psycopg://{user}:{password}@{host}:{port}/{dbname}"


def canonical_email(email: str) -> str:
    """Normalize email for uniqueness: strip +tags, dots for gmail, lowercase."""
    email = email.lower().strip()
    local, _, domain = email.partition("@")
    if not domain:
        return email
    # Strip +tag
    local = local.split("+")[0]
    # Gmail/Googlemail: strip dots
    if domain in ("gmail.com", "googlemail.com"):
        local = local.replace(".", "")
        domain = "gmail.com"
    return f"{local}@{domain}"


_VALID_ENTRY_TYPES = [e.value for e in EntryType]


def _parse_entry_type(entry_type: str | None) -> EntryType | None:
    """Parse entry_type string. Returns EntryType or None. Raises on invalid."""
    if not entry_type:
        return None
    try:
        return EntryType(entry_type)
    except ValueError:
        _validate_enum(entry_type, "entry_type", _VALID_ENTRY_TYPES)
        raise  # pragma: no cover — unreachable, _validate_enum always raises


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


def _paginate(
    items: list[Any],
    limit: int,
    offset: int | None,
) -> dict[str, Any]:
    """Apply limit+1 pattern: trim to limit, set has_more flag.

    Callers should fetch ``limit + 1`` rows from the store, then pass
    all results here. If len(items) > limit, the extra row proves more
    data exists and is trimmed from the response.
    """
    has_more = len(items) > limit
    return {
        "entries": items[:limit],
        "limit": limit,
        "offset": offset or 0,
        "has_more": has_more,
    }


def _levenshtein(a: str, b: str) -> int:
    """Compute Levenshtein edit distance between two strings."""
    if len(a) < len(b):
        return _levenshtein(b, a)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a):
        curr = [i + 1]
        for j, cb in enumerate(b):
            curr.append(
                min(
                    prev[j + 1] + 1,
                    curr[j] + 1,
                    prev[j] + (ca != cb),
                )
            )
        prev = curr
    return prev[-1]


def _suggest(value: str, valid_values: set[str] | list[str]) -> str | None:
    """Return the closest valid value if edit distance <= 2, else None.

    Returns None for exact matches (the value is already valid).
    Case-insensitive comparison — suggestion returned in its original case.
    """
    if value in valid_values:
        return None  # exact match (already valid)
    lower_value = value.lower()
    best: str | None = None
    best_dist = 3  # threshold: only suggest if distance <= 2
    for v in valid_values:
        dist = _levenshtein(lower_value, v.lower())
        if dist < best_dist:
            best_dist = dist
            best = v
    return best


def _error_response(
    code: str,
    message: str,
    *,
    retryable: bool,
    param: str | None = None,
    value: Any | None = None,
    valid: list[str] | None = None,
    suggestion: str | None = None,
    help_url: str | None = None,
) -> NoReturn:
    """Build a structured error envelope and raise ToolError.

    The MCP SDK wraps ToolError in a CallToolResult with isError=True,
    so clients get proper error signaling. The JSON envelope provides
    structured fields for smart clients alongside a human-readable message.

    Raises:
        ToolError: always — this function never returns.
    """
    from mcp.server.fastmcp.exceptions import ToolError

    error: dict[str, Any] = {
        "code": code,
        "message": message,
        "retryable": retryable,
    }
    if param is not None:
        error["param"] = param
    if value is not None:
        error["value"] = value
    if valid is not None:
        error["valid"] = valid
    if suggestion is not None:
        error["suggestion"] = suggestion
    if help_url is not None:
        error["help_url"] = help_url

    raise ToolError(json.dumps({"status": "error", "error": error}))


ISO_8601_HELP = "https://en.wikipedia.org/wiki/ISO_8601"


def _validate_enum(value: str, param: str, valid_values: set[str] | list[str]) -> None:
    """Raise structured error if value is not in valid_values.

    Includes did-you-mean suggestion if the value is close to a valid one.
    """
    if value in valid_values:
        return
    sorted_valid = sorted(valid_values)
    suggestion = _suggest(value, valid_values)
    msg = f"Invalid {param}: '{value}'. Valid: {', '.join(repr(v) for v in sorted_valid)}"
    if suggestion:
        msg += f". Did you mean '{suggestion}'?"
    _error_response(
        "invalid_parameter",
        msg,
        retryable=False,
        param=param,
        value=value,
        valid=sorted_valid,
        suggestion=suggestion,
    )


def _validate_timestamp(value: str | None, param: str) -> Any:
    """Validate and parse an ISO 8601 timestamp parameter.

    Returns parsed datetime if valid, None if value is None.
    Raises structured error for empty strings or unparseable values.
    """
    if value is None:
        return None
    if not value:
        _error_response(
            "invalid_parameter",
            f"{param} cannot be empty; omit or provide an ISO 8601 timestamp ({ISO_8601_HELP})",
            retryable=False,
            param=param,
            value="",
            help_url=ISO_8601_HELP,
        )
    from .schema import ensure_dt

    try:
        return ensure_dt(value)
    except (ValueError, TypeError):
        _error_response(
            "invalid_parameter",
            f"Invalid timestamp for '{param}': '{value}'."
            f" Provide an ISO 8601 timestamp ({ISO_8601_HELP})",
            retryable=False,
            param=param,
            value=value,
            help_url=ISO_8601_HELP,
        )


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
