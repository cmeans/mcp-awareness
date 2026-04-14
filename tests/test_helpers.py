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

import psycopg
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
    dsn_to_sqlalchemy_url,
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


class TestDsnToSqlalchemyUrl:
    """Test DSN-to-SQLAlchemy URL conversion used by alembic/env.py."""

    def test_plain_dsn(self):
        dsn = "host=db.local dbname=awareness user=admin password=secret port=5432"
        assert dsn_to_sqlalchemy_url(dsn) == (
            "postgresql+psycopg://admin:secret@db.local:5432/awareness"
        )

    def test_dsn_defaults(self):
        """Missing keys get sensible defaults."""
        assert dsn_to_sqlalchemy_url("host=myhost") == (
            "postgresql+psycopg://awareness:@myhost:5432/awareness"
        )

    def test_dsn_quoted_password_with_spaces(self):
        dsn = "host=localhost dbname=db user=u password='my secret'"
        assert dsn_to_sqlalchemy_url(dsn) == (
            "postgresql+psycopg://u:my%20secret@localhost:5432/db"
        )

    def test_dsn_password_with_at_sign(self):
        dsn = "host=localhost dbname=db user=u password='p@ss'"
        assert dsn_to_sqlalchemy_url(dsn) == ("postgresql+psycopg://u:p%40ss@localhost:5432/db")

    def test_dsn_password_with_slash(self):
        dsn = "host=localhost dbname=db user=u password='a/b'"
        assert dsn_to_sqlalchemy_url(dsn) == ("postgresql+psycopg://u:a%2Fb@localhost:5432/db")

    def test_dsn_escaped_quote_in_password(self):
        dsn = r"host=localhost dbname=db user=u password='it\'s'"
        assert dsn_to_sqlalchemy_url(dsn) == ("postgresql+psycopg://u:it%27s@localhost:5432/db")

    def test_url_passthrough_postgresql_psycopg(self):
        url = "postgresql+psycopg://u:p@h:5432/db"
        assert dsn_to_sqlalchemy_url(url) == url

    def test_url_passthrough_postgresql_plain(self):
        """postgresql:// is rewritten to postgresql+psycopg://."""
        url = "postgresql://u:p@h:5432/db"
        assert dsn_to_sqlalchemy_url(url) == "postgresql+psycopg://u:p@h:5432/db"

    def test_url_ambiguous_at_in_password_raises(self):
        """Unencoded @ in password makes URL ambiguous — must raise."""
        with pytest.raises(ValueError, match="unencoded '@'"):
            dsn_to_sqlalchemy_url("postgresql://u:p@ss@h:5432/db")

    def test_url_encoded_at_in_password_ok(self):
        """Properly percent-encoded @ in password passes through."""
        url = "postgresql+psycopg://u:p%40ss@h:5432/db"
        assert dsn_to_sqlalchemy_url(url) == url

    def test_whitespace_stripped(self):
        dsn = "  host=localhost dbname=db  "
        assert dsn_to_sqlalchemy_url(dsn) == ("postgresql+psycopg://awareness:@localhost:5432/db")

    def test_unquoted_special_chars_encoded(self):
        """Unquoted password with URL-special chars gets encoded."""
        dsn = "host=localhost dbname=db user=u password=p%ss"
        assert dsn_to_sqlalchemy_url(dsn) == ("postgresql+psycopg://u:p%25ss@localhost:5432/db")

    def test_extra_params_forwarded(self):
        """sslmode and other extra DSN params become URL query string."""
        dsn = "host=db dbname=mydb user=u password=p port=5432 sslmode=require"
        result = dsn_to_sqlalchemy_url(dsn)
        assert result.startswith("postgresql+psycopg://u:p@db:5432/mydb?")
        assert "sslmode=require" in result

    def test_multiple_extra_params(self):
        dsn = "host=db dbname=mydb user=u password=p connect_timeout=10 sslmode=verify-full"
        result = dsn_to_sqlalchemy_url(dsn)
        assert "sslmode=verify-full" in result
        assert "connect_timeout=10" in result


class TestErrorResponseExtras:
    """Test that _error_response merges **extras into the error envelope."""

    def test_extras_appear_in_payload(self):
        """Extra keyword arguments must be present in the raised ToolError JSON."""
        from mcp.server.fastmcp.exceptions import ToolError

        with pytest.raises(ToolError) as excinfo:
            _error_response(
                "schema_not_found",
                "No matching schema",
                retryable=False,
                schema_ref="schema:thing",
                schema_version="1.0.0",
                searched_owners=["alice", "_system"],
            )
        payload = json.loads(str(excinfo.value))
        err = payload["error"]
        assert err["code"] == "schema_not_found"
        assert err["schema_ref"] == "schema:thing"
        assert err["schema_version"] == "1.0.0"
        assert err["searched_owners"] == ["alice", "_system"]

    def test_extras_do_not_override_fixed_fields(self):
        """Extras cannot clobber the mandatory fixed fields."""
        from mcp.server.fastmcp.exceptions import ToolError

        with pytest.raises(ToolError) as excinfo:
            _error_response(
                "some_error",
                "Some message",
                retryable=True,
                extra_field="extra_value",
            )
        err = json.loads(str(excinfo.value))["error"]
        assert err["code"] == "some_error"
        assert err["message"] == "Some message"
        assert err["retryable"] is True
        assert err["extra_field"] == "extra_value"

    def test_no_extras_still_works(self):
        """Calling without extras should behave as before."""
        from mcp.server.fastmcp.exceptions import ToolError

        with pytest.raises(ToolError) as excinfo:
            _error_response("plain_error", "Plain message", retryable=False)
        err = json.loads(str(excinfo.value))["error"]
        assert err["code"] == "plain_error"
        assert "schema_ref" not in err

    def test_unix_socket_host(self):
        """Unix socket path goes in query string, not netloc."""
        dsn = "host=/var/run/postgresql dbname=db user=u"
        result = dsn_to_sqlalchemy_url(dsn)
        assert "host=%2Fvar%2Frun%2Fpostgresql" in result
        # netloc should have empty host
        assert "://u:@:5432/db?" in result

    def test_empty_raises(self):
        with pytest.raises(ValueError, match="must not be empty"):
            dsn_to_sqlalchemy_url("")

    def test_whitespace_only_raises(self):
        with pytest.raises(ValueError, match="must not be empty"):
            dsn_to_sqlalchemy_url("   ")

    def test_garbage_raises(self):
        with pytest.raises(psycopg.ProgrammingError):
            dsn_to_sqlalchemy_url("garbage")


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
