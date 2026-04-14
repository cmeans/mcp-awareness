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

"""Validation helpers for Schema and Record entry types.

Pure functions wrapping jsonschema Draft 2020-12 validation and schema
lookup with _system fallback. Kept out of the store layer so the Store
protocol stays swappable (no jsonschema import in store.py).
"""

from __future__ import annotations


def compose_schema_logical_key(family: str, version: str) -> str:
    """Derive the canonical logical_key for a schema entry.

    Single source of truth for the family+version → logical_key format.
    Used by register_schema on write and by resolve_schema on lookup.
    """
    return f"{family}:{version}"
