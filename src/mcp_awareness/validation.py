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

from typing import Any

from jsonschema import Draft202012Validator, ValidationError


def compose_schema_logical_key(family: str, version: str) -> str:
    """Derive the canonical logical_key for a schema entry.

    Single source of truth for the family+version → logical_key format.
    Used by register_schema on write and by resolve_schema on lookup.
    """
    return f"{family}:{version}"


def validate_schema_body(schema: Any) -> None:
    """Validate a schema body against the JSON Schema Draft 2020-12 meta-schema.

    Raises jsonschema.exceptions.SchemaError on invalid schema. Callers at
    the MCP boundary translate this into a structured 'invalid_schema' error
    response; direct callers (CLI) format to stderr.
    """
    Draft202012Validator.check_schema(schema)


_MAX_VALIDATION_ERRORS = 50


def _flatten_error(err: ValidationError) -> dict[str, Any]:
    """Flatten a jsonschema ValidationError to a structured dict for the error envelope."""
    return {
        "path": err.json_path,
        "message": err.message,
        "validator": err.validator,
        "schema_path": "/" + "/".join(str(p) for p in err.schema_path),
    }


def validate_record_content(schema_body: dict[str, Any], content: Any) -> list[dict[str, Any]]:
    """Validate content against a schema body. Returns list of structured errors.

    Empty list means valid. List truncated at _MAX_VALIDATION_ERRORS; when
    truncated, final entry is {'truncated': True, 'total_errors': <n>}.
    """
    validator = Draft202012Validator(schema_body)
    all_errors = sorted(validator.iter_errors(content), key=lambda e: e.path)
    if len(all_errors) <= _MAX_VALIDATION_ERRORS:
        return [_flatten_error(e) for e in all_errors]
    kept = [_flatten_error(e) for e in all_errors[:_MAX_VALIDATION_ERRORS]]
    kept.append({"truncated": True, "total_errors": len(all_errors)})
    return kept
