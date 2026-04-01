# Gzip Compression + Pagination Improvements Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add gzip response compression and improve pagination with accurate `has_more` signaling and a lower default limit.

**Architecture:** GZipMiddleware wraps the outermost ASGI layer. A new `_paginate` helper in `helpers.py` applies the limit+1 pattern and builds pagination metadata. Each paginated tool uses the helper to include `limit`, `offset`, and `has_more` in responses.

**Tech Stack:** Starlette GZipMiddleware (already a dependency), no new packages.

---

### Task 1: Lower DEFAULT_QUERY_LIMIT and add pagination helper

**Files:**
- Modify: `src/mcp_awareness/helpers.py:40` (constant) and add `_paginate` helper
- Test: `tests/test_helpers.py` (new file or append)

- [ ] **Step 1: Write tests for the pagination helper**

Create `tests/test_helpers.py` (or find existing):

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_helpers.py -v`
Expected: FAIL — `_paginate` does not exist, `DEFAULT_QUERY_LIMIT` is 200

- [ ] **Step 3: Implement changes in helpers.py**

In `src/mcp_awareness/helpers.py`:

Change line 40:
```python
DEFAULT_QUERY_LIMIT = 100
```

Add after `_validate_pagination` (after line 84):
```python
def _paginate(
    items: list[Any],
    limit: int,
    offset: int | None,
) -> dict[str, Any]:
    """Apply limit+1 pattern: trim to limit, set has_more flag.

    Callers should fetch ``limit + 1`` rows from the store, then pass
    all results here. If len(items) > limit, the extra row proves more
    data exists and is trimmed from the response.
    """
    has_more = len(items) > limit
    return {
        "entries": items[:limit],
        "limit": limit,
        "offset": offset or 0,
        "has_more": has_more,
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_helpers.py -v`
Expected: All PASS

- [ ] **Step 5: Lint and typecheck**

Run: `ruff check src/mcp_awareness/helpers.py tests/test_helpers.py && mypy src/mcp_awareness/helpers.py`

- [ ] **Step 6: Commit**

```bash
git add src/mcp_awareness/helpers.py tests/test_helpers.py
git commit -m "feat: lower DEFAULT_QUERY_LIMIT to 100 and add _paginate helper"
```

---

### Task 2: Update paginated tools to use _paginate and fetch limit+1

**Files:**
- Modify: `src/mcp_awareness/tools.py` — all 9 paginated tools
- Test: `tests/test_tools.py` or `tests/test_integration.py` — pagination metadata tests

There are two patterns in tools.py:

**Pattern A** — tools using `_validate_pagination` (have both limit+offset): `get_alerts`, `get_knowledge`, `get_deleted`
**Pattern B** — tools with manual `if limit is None: limit = DEFAULT_QUERY_LIMIT` (limit only): `get_reads`, `get_actions`, `get_unread`, `get_activity`, `get_intentions`
**Pattern C** — `semantic_search` (own default of 10, max 100, no offset)

All three patterns need the same treatment: fetch `limit + 1`, pass through `_paginate`, return the metadata dict.

- [ ] **Step 1: Write tests for pagination metadata in tool responses**

Add to the appropriate test file (likely `tests/test_integration.py` or a new `tests/test_pagination.py`). These tests need a running store, so use the `store` fixture from existing tests.

```python
import json
import pytest
from mcp_awareness.helpers import _paginate


class TestPaginationMetadata:
    """Verify paginated tools return has_more metadata."""

    @pytest.fixture(autouse=True)
    def seed_entries(self, store, owner):
        """Create 5 notes so pagination can be exercised."""
        from mcp_awareness.schema import Entry, EntryType
        from tests.conftest import make_id, now_utc

        for i in range(5):
            store.add(
                owner,
                Entry(
                    id=make_id(),
                    type=EntryType.NOTE,
                    source="test",
                    tags=["pagination-test"],
                    created=now_utc(),
                    expires=None,
                    data={"description": f"note-{i}"},
                ),
            )

    async def test_get_knowledge_has_more_true(self, call_tool):
        result = json.loads(await call_tool("get_knowledge", tags=["pagination-test"], limit=3))
        assert result["has_more"] is True
        assert len(result["entries"]) == 3
        assert result["limit"] == 3
        assert result["offset"] == 0

    async def test_get_knowledge_has_more_false(self, call_tool):
        result = json.loads(await call_tool("get_knowledge", tags=["pagination-test"], limit=10))
        assert result["has_more"] is False
        assert len(result["entries"]) == 5
```

Note: The exact test fixture names (`call_tool`, `store`, `owner`) should match the existing test infrastructure. Check `tests/conftest.py` for available fixtures and adapt accordingly.

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_pagination.py -v`
Expected: FAIL — tools return a list, not a dict with `has_more`

- [ ] **Step 3: Update Pattern A tools (get_alerts, get_knowledge, get_deleted)**

For each tool, the change is the same pattern. Example for `get_alerts` in `tools.py`:

Before:
```python
    pv = _validate_pagination(limit, offset)
    if isinstance(pv, str):
        return json.dumps({"error": pv})
    limit, offset = pv
    # ... fetch ...
    alerts = _srv.store.get_active_alerts(
        _srv._owner_id(), source, since=since_dt, limit=limit, offset=offset
    )
    _srv._log_reads(alerts, "get_alerts")
    if mode == "list":
        return json.dumps([a.to_list_dict() for a in alerts], indent=2)
    return json.dumps([a.to_dict() for a in alerts], indent=2)
```

After:
```python
    pv = _validate_pagination(limit, offset)
    if isinstance(pv, str):
        return json.dumps({"error": pv})
    limit, offset = pv
    # ... fetch limit+1 ...
    alerts = _srv.store.get_active_alerts(
        _srv._owner_id(), source, since=since_dt, limit=limit + 1, offset=offset
    )
    _srv._log_reads(alerts[:limit], "get_alerts")
    if mode == "list":
        page = _paginate([a.to_list_dict() for a in alerts], limit, offset)
    else:
        page = _paginate([a.to_dict() for a in alerts], limit, offset)
    return json.dumps(page, indent=2)
```

Apply the same pattern to `get_knowledge` and `get_deleted`. For `get_knowledge`, the semantic re-ranking block happens before the return — apply `_paginate` after re-ranking.

- [ ] **Step 4: Update Pattern B tools (get_reads, get_actions, get_unread, get_activity, get_intentions)**

These don't use `_validate_pagination` — they have inline `if limit is None: limit = DEFAULT_QUERY_LIMIT`. Apply the same limit+1 fetch and `_paginate` wrapper.

Example for `get_reads`:

Before:
```python
    if limit is None:
        limit = DEFAULT_QUERY_LIMIT
    # ... fetch ...
    reads = _srv.store.get_reads(
        _srv._owner_id(), entry_id=entry_id, since=since_dt, platform=platform, limit=limit
    )
    return json.dumps(reads, indent=2)
```

After:
```python
    if limit is None:
        limit = DEFAULT_QUERY_LIMIT
    # ... fetch limit+1 ...
    reads = _srv.store.get_reads(
        _srv._owner_id(), entry_id=entry_id, since=since_dt, platform=platform, limit=limit + 1
    )
    page = _paginate(reads, limit, None)
    return json.dumps(page, indent=2)
```

Note: `get_reads`, `get_actions`, and `get_activity` return dicts (not Entry objects), so pass the list directly to `_paginate`. `get_unread` and `get_intentions` return Entry objects — use `to_list_dict()` / `to_dict()` as they currently do, then pass the dict list to `_paginate`.

- [ ] **Step 5: Update Pattern C (semantic_search)**

`semantic_search` uses its own `limit = max(1, min(limit, 100))` clamping and returns `(entry, score)` tuples. Apply limit+1 to the store call and `_paginate` to the output.

Before:
```python
    limit = max(1, min(limit, 100))
    # ... fetch ...
    results = _srv.store.semantic_search(
        _srv._owner_id(),
        embedding=vectors[0],
        model=provider.model_name,
        # ...
        limit=limit,
    )
    # ... format results ...
```

After:
```python
    limit = max(1, min(limit, 100))
    # ... fetch limit+1 ...
    results = _srv.store.semantic_search(
        _srv._owner_id(),
        embedding=vectors[0],
        model=provider.model_name,
        # ...
        limit=limit + 1,
    )
    _srv._log_reads([e for e, _ in results[:limit]], "semantic_search")
    if mode == "list":
        items = []
        for entry, score in results:
            d = entry.to_list_dict()
            d["similarity"] = round(score, 4)
            items.append(d)
    else:
        items = []
        for entry, score in results:
            d = entry.to_dict()
            d["similarity"] = round(score, 4)
            items.append(d)
    page = _paginate(items, limit, None)
    return json.dumps(page, indent=2)
```

- [ ] **Step 6: Add _paginate import to tools.py**

At the top of `tools.py`, update the import from helpers:

```python
from .helpers import (
    DEFAULT_QUERY_LIMIT,
    _paginate,
    _parse_entry_type,
    _timed,
    _validate_pagination,
)
```

- [ ] **Step 7: Run all tests**

Run: `pytest tests/ -v`
Expected: New pagination tests pass. Some existing tests may need updating if they assert on the response shape (list vs dict with `entries` key). Fix any failures.

- [ ] **Step 8: Lint and typecheck**

Run: `ruff check src/mcp_awareness/tools.py && mypy src/mcp_awareness/tools.py`

- [ ] **Step 9: Commit**

```bash
git add src/mcp_awareness/tools.py tests/
git commit -m "feat: add has_more pagination metadata to all paginated tools"
```

---

### Task 3: Add GZipMiddleware to HTTP transport

**Files:**
- Modify: `src/mcp_awareness/server.py:408-468` (both HTTP transport paths)
- Test: `tests/test_middleware.py` — gzip tests

- [ ] **Step 1: Write tests for gzip compression**

Add to `tests/test_middleware.py`:

```python
class TestGZipCompression:
    """GZipMiddleware compresses HTTP responses."""

    @pytest.mark.anyio
    async def test_gzip_applied_when_requested(self) -> None:
        """Response is gzip-compressed when client sends Accept-Encoding: gzip."""
        from starlette.middleware.gzip import GZipMiddleware

        # Create a test app that returns a large-enough response
        async def big_app(scope, receive, send):
            body = b"x" * 1000
            await send({"type": "http.response.start", "status": 200, "headers": [
                (b"content-type", b"application/json"),
            ]})
            await send({"type": "http.response.body", "body": body})

        app = GZipMiddleware(big_app, minimum_size=500)
        scope = {
            "type": "http",
            "method": "GET",
            "path": "/test",
            "headers": [(b"accept-encoding", b"gzip")],
        }
        status, body, headers = await _collect_response_with_headers(app, scope)
        assert status == 200
        header_dict = dict(headers)
        assert header_dict.get(b"content-encoding") == b"gzip"

    @pytest.mark.anyio
    async def test_gzip_not_applied_without_accept_encoding(self) -> None:
        """Response is NOT compressed when client doesn't request gzip."""
        from starlette.middleware.gzip import GZipMiddleware

        async def big_app(scope, receive, send):
            body = b"x" * 1000
            await send({"type": "http.response.start", "status": 200, "headers": [
                (b"content-type", b"application/json"),
            ]})
            await send({"type": "http.response.body", "body": body})

        app = GZipMiddleware(big_app, minimum_size=500)
        scope = {
            "type": "http",
            "method": "GET",
            "path": "/test",
            "headers": [],
        }
        status, body, headers = await _collect_response_with_headers(app, scope)
        assert status == 200
        header_dict = dict(headers)
        assert header_dict.get(b"content-encoding") is None

    @pytest.mark.anyio
    async def test_gzip_skips_small_responses(self) -> None:
        """Responses under 500 bytes are NOT compressed."""
        from starlette.middleware.gzip import GZipMiddleware

        async def small_app(scope, receive, send):
            body = b"small"
            await send({"type": "http.response.start", "status": 200, "headers": [
                (b"content-type", b"application/json"),
            ]})
            await send({"type": "http.response.body", "body": body})

        app = GZipMiddleware(small_app, minimum_size=500)
        scope = {
            "type": "http",
            "method": "GET",
            "path": "/test",
            "headers": [(b"accept-encoding", b"gzip")],
        }
        status, body, headers = await _collect_response_with_headers(app, scope)
        assert status == 200
        header_dict = dict(headers)
        assert header_dict.get(b"content-encoding") is None
```

Note: You'll need a `_collect_response_with_headers` helper that captures headers too. Check the existing `_collect_response` in `test_middleware.py` and extend it.

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_middleware.py::TestGZipCompression -v`
Expected: FAIL — helper doesn't exist yet

- [ ] **Step 3: Add `_collect_response_with_headers` helper**

In `tests/test_middleware.py`, add alongside existing `_collect_response`:

```python
async def _collect_response_with_headers(
    app: Any, scope: dict[str, Any]
) -> tuple[int, bytes, list[tuple[bytes, bytes]]]:
    """Send a request through an ASGI app, return (status, body, headers)."""
    status = 0
    body = b""
    headers: list[tuple[bytes, bytes]] = []

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b""}

    async def send(message: dict[str, Any]) -> None:
        nonlocal status, body, headers
        if message["type"] == "http.response.start":
            status = message["status"]
            headers = message.get("headers", [])
        elif message["type"] == "http.response.body":
            body += message.get("body", b"")

    await app(scope, receive, send)
    return status, body, headers
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_middleware.py::TestGZipCompression -v`
Expected: All PASS (these test GZipMiddleware directly, which already works)

- [ ] **Step 5: Wire GZipMiddleware into server.py**

In `src/mcp_awareness/server.py`, in the `_run()` function, wrap both HTTP transport paths with GZipMiddleware as the outermost layer.

For the MOUNT_PATH path (around line 431):
```python
        app = _wrap_with_auth(app)

        from starlette.middleware.gzip import GZipMiddleware
        app = GZipMiddleware(app, minimum_size=500)

        config = uvicorn.Config(app, host=HOST, port=PORT)
```

For the no-MOUNT_PATH path (around line 459):
```python
        health_app = _wrap_with_auth(health_app)

        from starlette.middleware.gzip import GZipMiddleware
        health_app = GZipMiddleware(health_app, minimum_size=500)

        config = uvicorn.Config(health_app, host=HOST, port=PORT)
```

- [ ] **Step 6: Add wiring test**

Add to `tests/test_middleware.py` (in the existing `TestRunTransportWiring` class or new class):

```python
def test_http_transport_uses_gzip_middleware(self):
    """HTTP transport wraps app with GZipMiddleware."""
    from starlette.middleware.gzip import GZipMiddleware
    # Verify the middleware is present by checking the type chain
    # (implementation depends on how _run builds the app — may need
    # to extract the app-building logic into a testable function)
```

Note: If `_run()` is not easily testable (it calls `uvicorn.Server.serve`), the gzip wiring can be verified via the integration-level tests or by extracting the app construction. Use your judgment — a unit test for the middleware behavior (Task 3, Step 1) plus the wiring in server.py may be sufficient.

- [ ] **Step 7: Lint and typecheck**

Run: `ruff check src/mcp_awareness/server.py tests/test_middleware.py && mypy src/mcp_awareness/server.py`

- [ ] **Step 8: Commit**

```bash
git add src/mcp_awareness/server.py tests/test_middleware.py
git commit -m "feat: add GZipMiddleware for HTTP response compression"
```

---

### Task 4: Update CHANGELOG and tool docstrings

**Files:**
- Modify: `CHANGELOG.md`
- Modify: `src/mcp_awareness/tools.py` — update docstrings mentioning default 200

- [ ] **Step 1: Update CHANGELOG.md**

Add under `## [Unreleased]`:

```markdown
### Changed
- **Default query limit reduced**: `DEFAULT_QUERY_LIMIT` lowered from 200 to 100 — reduces default response size for all paginated tools
- **Pagination metadata**: all paginated tools now return `{entries, limit, offset, has_more}` instead of a bare list — agents can detect when more data exists without a separate count query

### Added
- **Gzip response compression**: HTTP transport now compresses responses over 500 bytes via Starlette GZipMiddleware — expected 60-80% egress reduction for typical payloads
```

- [ ] **Step 2: Update tool docstrings that mention "default 200"**

Search for "200" in tool docstrings in `tools.py` and update to "100". Specifically `get_unread` docstring mentions "default 200" at line 864.

- [ ] **Step 3: Lint**

Run: `ruff check CHANGELOG.md src/mcp_awareness/tools.py`

- [ ] **Step 4: Commit**

```bash
git add CHANGELOG.md src/mcp_awareness/tools.py
git commit -m "docs: update CHANGELOG and docstrings for pagination and gzip changes"
```

---

### Task 5: Fix existing tests broken by response shape change

**Files:**
- Modify: various test files that assert on paginated tool response shapes

- [ ] **Step 1: Run full test suite**

Run: `pytest tests/ -v`
Expected: Some tests may fail because they expect a list `[...]` but now get `{"entries": [...], "has_more": ...}`

- [ ] **Step 2: Fix each failing test**

For each test that parses a paginated tool response as a list, update to extract from `result["entries"]`. Example:

Before:
```python
result = json.loads(await call_tool("get_knowledge", tags=["test"]))
assert len(result) == 5
```

After:
```python
result = json.loads(await call_tool("get_knowledge", tags=["test"]))
assert len(result["entries"]) == 5
```

- [ ] **Step 3: Run full test suite again**

Run: `pytest tests/ -v`
Expected: All PASS

- [ ] **Step 4: Lint and typecheck**

Run: `ruff check src/ tests/ && mypy src/mcp_awareness/`

- [ ] **Step 5: Commit**

```bash
git add tests/
git commit -m "test: update existing tests for new pagination response shape"
```

---

### Task 6: Final verification

- [ ] **Step 1: Run full test suite with coverage**

Run: `pytest tests/ -v --cov=mcp_awareness`
Expected: All pass, coverage stable or improved

- [ ] **Step 2: Lint and typecheck entire project**

Run: `ruff check src/ tests/ && mypy src/mcp_awareness/`

- [ ] **Step 3: Verify test count**

Count total tests and note the number for README/status updates.
