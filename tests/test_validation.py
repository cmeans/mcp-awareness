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

from mcp_awareness.validation import (
    SchemaInUseError,
    assert_schema_deletable,
    compose_schema_logical_key,
    resolve_schema,
    validate_record_content,
    validate_schema_body,
)

_PERSON_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "age": {"type": "integer", "minimum": 0},
    },
    "required": ["name"],
}


def test_compose_schema_logical_key_basic():
    assert (
        compose_schema_logical_key("schema:edge-manifest", "1.0.0") == "schema:edge-manifest:1.0.0"
    )


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


class _StubStore:
    """Minimal Store-like stub for validation unit tests.

    Records calls to find_schema and returns pre-configured results keyed by
    (owner_id, logical_key). Only needs to implement find_schema; other Store
    methods are never called by resolve_schema.
    """

    def __init__(self):
        self._results: dict[tuple[str, str], object] = {}
        self.calls: list[tuple[str, str]] = []

    def set(self, owner_id: str, logical_key: str, result):
        self._results[(owner_id, logical_key)] = result

    def find_schema(self, owner_id, logical_key):
        self.calls.append((owner_id, logical_key))
        return self._results.get((owner_id, logical_key))


def test_resolve_schema_delegates_to_find_schema():
    stub = _StubStore()
    sentinel = object()
    stub.set("alice", "s:test:1.0.0", sentinel)
    result = resolve_schema(stub, "alice", "s:test", "1.0.0")
    assert result is sentinel


def test_resolve_schema_returns_none_when_missing():
    stub = _StubStore()
    assert resolve_schema(stub, "alice", "s:nope", "1.0.0") is None


def test_resolve_schema_composes_logical_key_correctly():
    """Confirms family+version are composed via compose_schema_logical_key."""
    stub = _StubStore()
    resolve_schema(stub, "alice", "schema:edge-manifest", "2.3.4")
    assert stub.calls == [("alice", "schema:edge-manifest:2.3.4")]


class _CounterStore:
    """Stub exposing count_records_referencing for assert_schema_deletable tests."""

    def __init__(self, count: int, ids: list[str]):
        self._count = count
        self._ids = ids

    def count_records_referencing(self, owner_id, schema_logical_key):
        return (self._count, self._ids)


def test_assert_schema_deletable_passes_with_zero_refs():
    # Must not raise
    assert_schema_deletable(_CounterStore(0, []), "alice", "s:test:1.0.0")


def test_assert_schema_deletable_raises_with_refs():
    with pytest.raises(SchemaInUseError) as excinfo:
        assert_schema_deletable(_CounterStore(3, ["id1", "id2", "id3"]), "alice", "s:test:1.0.0")
    assert excinfo.value.total_count == 3
    assert excinfo.value.referencing_records == ["id1", "id2", "id3"]


def test_schema_in_use_error_has_readable_message():
    err = SchemaInUseError(total_count=5, referencing_records=["a", "b"])
    assert "5" in str(err)
