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

"""OAuth proxy workaround for Claude Desktop/Claude.ai bugs.

Claude.ai ignores ``authorization_endpoint`` and ``token_endpoint`` from
OAuth metadata and instead constructs URLs from the MCP server's base URL.
This middleware intercepts those requests and forwards them to the real
OAuth provider (e.g. WorkOS AuthKit).

Feature-gated via ``AWARENESS_OAUTH_PROXY=true``.  Designed for removal
once the upstream bugs are fixed.
"""

from __future__ import annotations

import logging

from starlette.types import Scope

logger = logging.getLogger(__name__)

# Default trusted-header chain — Cloudflare environment
_DEFAULT_IP_HEADERS = ["CF-Connecting-IP", "X-Real-IP"]


def resolve_client_ip(
    scope: Scope,
    *,
    ip_headers: list[str] | None = None,
) -> str:
    """Extract the real client IP from trusted proxy headers.

    Walks ``ip_headers`` in order (default: CF-Connecting-IP, X-Real-IP),
    returns the first non-empty value.  Falls back to the ASGI ``client``
    address if no trusted header is found.
    """
    chain = ip_headers if ip_headers is not None else _DEFAULT_IP_HEADERS
    raw_headers = dict(scope.get("headers", []))

    for header_name in chain:
        value = raw_headers.get(header_name.lower().encode(), b"").decode().strip()
        if value:
            return value

    client = scope.get("client")
    if client:
        return client[0]
    return "unknown"
