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

"""Tests for src/mcp_awareness/validation.py."""

from __future__ import annotations

import jsonschema
import pytest

from mcp_awareness.validation import compose_schema_logical_key, validate_schema_body


def test_compose_schema_logical_key_basic():
    assert compose_schema_logical_key("schema:edge-manifest", "1.0.0") == "schema:edge-manifest:1.0.0"


def test_compose_schema_logical_key_no_prefix():
    assert compose_schema_logical_key("tag-taxonomy", "0.1.0") == "tag-taxonomy:0.1.0"


def test_validate_schema_body_accepts_valid_object_schema():
    schema = {
        "type": "object",
        "properties": {"name": {"type": "string"}},
        "required": ["name"],
    }
    validate_schema_body(schema)  # must not raise


def test_validate_schema_body_rejects_bad_type():
    schema = {"type": "strng"}  # typo: 'strng' is not a valid JSON Schema type
    with pytest.raises(jsonschema.exceptions.SchemaError):
        validate_schema_body(schema)


def test_validate_schema_body_accepts_empty_object():
    # Empty schema matches anything — valid per spec
    validate_schema_body({})


def test_validate_schema_body_rejects_non_dict():
    # Schemas must be objects; bare arrays fail meta-schema
    with pytest.raises(jsonschema.exceptions.SchemaError):
        validate_schema_body([{"type": "string"}])  # type: ignore[arg-type]
