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

from mcp_awareness.helpers import DEFAULT_QUERY_LIMIT, _paginate


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
