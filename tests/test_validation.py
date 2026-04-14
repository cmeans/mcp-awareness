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

from mcp_awareness.validation import compose_schema_logical_key, validate_schema_body, validate_record_content


_PERSON_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "age": {"type": "integer", "minimum": 0},
    },
    "required": ["name"],
}


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


def test_validate_record_content_valid_returns_empty_list():
    assert validate_record_content(_PERSON_SCHEMA, {"name": "Alice", "age": 30}) == []


def test_validate_record_content_surfaces_missing_required():
    errors = validate_record_content(_PERSON_SCHEMA, {"age": 30})
    assert len(errors) == 1
    assert errors[0]["validator"] == "required"
    assert "name" in errors[0]["message"]


def test_validate_record_content_surfaces_all_errors():
    # Missing 'name' AND age is wrong type
    errors = validate_record_content(_PERSON_SCHEMA, {"age": "thirty"})
    assert len(errors) == 2
    validators = {e["validator"] for e in errors}
    assert validators == {"required", "type"}


def test_validate_record_content_is_sorted_by_path():
    schema = {
        "type": "object",
        "properties": {
            "a": {"type": "integer"},
            "b": {"type": "integer"},
            "c": {"type": "integer"},
        },
    }
    errors = validate_record_content(schema, {"a": "x", "b": "y", "c": "z"})
    paths = [e["path"] for e in errors]
    assert paths == sorted(paths)


def test_validate_record_content_accepts_primitive_schema():
    schema = {"type": "integer"}
    assert validate_record_content(schema, 42) == []
    errors = validate_record_content(schema, "abc")
    assert len(errors) == 1
    assert errors[0]["validator"] == "type"


def test_validate_record_content_array_schema_with_index_paths():
    schema = {"type": "array", "items": {"type": "integer"}}
    errors = validate_record_content(schema, [1, "two", 3, "four"])
    assert len(errors) == 2
    # Array indices should appear in paths
    paths = [e["path"] for e in errors]
    assert any("1" in p for p in paths)
    assert any("3" in p for p in paths)


def test_validate_record_content_truncates_at_50():
    schema = {
        "type": "array",
        "items": {"type": "integer"},
    }
    # 60 wrong-type items — all fail
    result = validate_record_content(schema, ["x"] * 60)
    assert isinstance(result, list)
    # Truncation is carried via a special sentinel entry at the end
    assert len(result) == 51  # 50 errors + 1 truncation marker
    assert result[-1]["truncated"] is True
    assert result[-1]["total_errors"] == 60
