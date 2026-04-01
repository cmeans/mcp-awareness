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

from mcp_awareness.helpers import DEFAULT_QUERY_LIMIT, _levenshtein, _paginate, _suggest


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
