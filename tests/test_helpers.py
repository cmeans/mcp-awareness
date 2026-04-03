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

import json

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from mcp_awareness.helpers import (
    DEFAULT_QUERY_LIMIT,
    _error_response,
    _levenshtein,
    _paginate,
    _suggest,
    _validate_enum,
    _validate_timestamp,
)


def test_default_query_limit_is_100():
    assert DEFAULT_QUERY_LIMIT == 100


def test_paginate_has_more_true():
    """When results == limit+1, has_more is true and extra item trimmed."""
    items = list(range(11))  # 11 items fetched with limit=10
    result = _paginate(items, limit=10, offset=0)
    assert result["has_more"] is True
    assert len(result["entries"]) == 10
    assert result["limit"] == 10
    assert result["offset"] == 0


def test_paginate_has_more_false():
    """When results < limit+1, has_more is false."""
    items = list(range(7))
    result = _paginate(items, limit=10, offset=0)
    assert result["has_more"] is False
    assert len(result["entries"]) == 7
    assert result["limit"] == 10
    assert result["offset"] == 0


def test_paginate_exact_limit():
    """When results == limit exactly (not limit+1), has_more is false."""
    items = list(range(10))
    result = _paginate(items, limit=10, offset=0)
    assert result["has_more"] is False
    assert len(result["entries"]) == 10


def test_paginate_with_offset():
    """Offset is passed through in metadata."""
    items = list(range(5))
    result = _paginate(items, limit=10, offset=20)
    assert result["offset"] == 20
    assert result["has_more"] is False


def test_paginate_empty():
    """Empty results return has_more false."""
    result = _paginate([], limit=10, offset=0)
    assert result["entries"] == []
    assert result["has_more"] is False


class TestLevenshtein:
    def test_identical_strings(self):
        assert _levenshtein("note", "note") == 0

    def test_single_insert(self):
        assert _levenshtein("note", "notes") == 1

    def test_single_delete(self):
        assert _levenshtein("notes", "note") == 1

    def test_single_substitute(self):
        assert _levenshtein("note", "mote") == 1

    def test_completely_different(self):
        assert _levenshtein("abc", "xyz") == 3

    def test_empty_strings(self):
        assert _levenshtein("", "") == 0
        assert _levenshtein("abc", "") == 3
        assert _levenshtein("", "abc") == 3

    def test_case_sensitive(self):
        assert _levenshtein("Note", "note") == 1


class TestSuggest:
    def test_close_match_returned(self):
        assert _suggest("notes", {"note", "context", "preference", "pattern"}) == "note"

    def test_no_close_match_returns_none(self):
        assert _suggest("xyz", {"note", "context", "preference", "pattern"}) is None

    def test_exact_match_returns_none(self):
        assert _suggest("note", {"note", "context", "preference", "pattern"}) is None

    def test_case_mismatch_suggests(self):
        assert _suggest("Note", {"note", "context", "preference", "pattern"}) == "note"

    def test_plural_suggests_singular(self):
        assert _suggest("alerts", {"alert", "status", "pattern"}) == "alert"

    def test_distance_3_not_suggested(self):
        assert _suggest("abcdef", {"note", "context"}) is None

    def test_picks_closest_match(self):
        assert _suggest("contex", {"note", "context", "preference"}) == "context"


class TestErrorResponse:
    def test_raises_tool_error(self):
        with pytest.raises(ToolError):
            _error_response("invalid_parameter", "test message", retryable=False)

    def test_invalid_parameter_structure(self):
        with pytest.raises(ToolError) as exc_info:
            _error_response(
                "invalid_parameter",
                "Invalid entry_type: 'bogus'",
                retryable=False,
                param="entry_type",
                value="bogus",
                valid=["note", "context"],
            )
        body = json.loads(str(exc_info.value))
        assert body["status"] == "error"
        assert body["error"]["code"] == "invalid_parameter"
        assert body["error"]["message"] == "Invalid entry_type: 'bogus'"
        assert body["error"]["retryable"] is False
        assert body["error"]["param"] == "entry_type"
        assert body["error"]["value"] == "bogus"
        assert body["error"]["valid"] == ["note", "context"]

    def test_not_found_structure(self):
        with pytest.raises(ToolError) as exc_info:
            _error_response("not_found", "Not found", retryable=False, param="source", value="test")
        body = json.loads(str(exc_info.value))
        assert body["error"]["code"] == "not_found"
        assert "valid" not in body["error"]

    def test_unavailable_structure(self):
        with pytest.raises(ToolError) as exc_info:
            _error_response("unavailable", "Provider down", retryable=True)
        body = json.loads(str(exc_info.value))
        assert body["error"]["code"] == "unavailable"
        assert body["error"]["retryable"] is True
        assert "param" not in body["error"]

    def test_suggestion_included(self):
        with pytest.raises(ToolError) as exc_info:
            _error_response("invalid_parameter", "test", retryable=False, suggestion="note")
        body = json.loads(str(exc_info.value))
        assert body["error"]["suggestion"] == "note"

    def test_help_url_included(self):
        with pytest.raises(ToolError) as exc_info:
            _error_response(
                "invalid_parameter", "test", retryable=False, help_url="https://example.com"
            )
        body = json.loads(str(exc_info.value))
        assert body["error"]["help_url"] == "https://example.com"

    def test_no_optional_fields_when_omitted(self):
        with pytest.raises(ToolError) as exc_info:
            _error_response("unavailable", "down", retryable=True)
        body = json.loads(str(exc_info.value))
        assert set(body["error"].keys()) == {"code", "message", "retryable"}


class TestErrorConvenience:
    def test_validate_enum_valid_value(self):
        _validate_enum("note", "entry_type", {"note", "context", "preference", "pattern"})

    def test_validate_enum_invalid_raises(self):
        with pytest.raises(ToolError) as exc_info:
            _validate_enum("bogus", "entry_type", {"note", "context", "preference", "pattern"})
        body = json.loads(str(exc_info.value))
        assert body["error"]["code"] == "invalid_parameter"
        assert body["error"]["param"] == "entry_type"
        assert body["error"]["value"] == "bogus"
        assert "valid" in body["error"]

    def test_validate_enum_with_suggestion(self):
        with pytest.raises(ToolError) as exc_info:
            _validate_enum("notes", "entry_type", {"note", "context", "preference", "pattern"})
        body = json.loads(str(exc_info.value))
        assert body["error"]["suggestion"] == "note"
        assert "Did you mean" in body["error"]["message"]

    def test_validate_timestamp_empty_raises(self):
        with pytest.raises(ToolError) as exc_info:
            _validate_timestamp("", "since")
        body = json.loads(str(exc_info.value))
        assert body["error"]["code"] == "invalid_parameter"
        assert body["error"]["param"] == "since"
        assert "ISO 8601" in body["error"]["message"]
        assert body["error"]["help_url"] == "https://en.wikipedia.org/wiki/ISO_8601"

    def test_validate_timestamp_none_returns_none(self):
        assert _validate_timestamp(None, "since") is None

    def test_validate_timestamp_valid_returns_datetime(self):
        result = _validate_timestamp("2026-04-01T12:00:00Z", "since")
        assert result is not None

    def test_validate_timestamp_bad_format_raises(self):
        with pytest.raises(ToolError) as exc_info:
            _validate_timestamp("not-a-date", "since")
        body = json.loads(str(exc_info.value))
        assert body["error"]["code"] == "invalid_parameter"
        assert body["error"]["param"] == "since"
        assert body["error"]["help_url"] == "https://en.wikipedia.org/wiki/ISO_8601"
