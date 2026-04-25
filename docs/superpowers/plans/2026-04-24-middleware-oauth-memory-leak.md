<!-- SPDX-License-Identifier: AGPL-3.0-or-later | Copyright (C) 2026 Chris Means -->
# Middleware + OAuth Memory-Leak Cluster Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bound `AuthMiddleware._owner_semaphores` and `RateLimiter._hits`, and remove the `asyncio.Semaphore._value` private-attr read in the auth concurrency check.

**Architecture:** Replace the per-owner semaphore dict with an `OrderedDict[str, int]` of in-flight counts guarded by a single `asyncio.Lock`. Switch `RateLimiter._hits` to an `OrderedDict[str, list[float]]` with an LRU cap. Both data structures are hard-capped at 10,000 entries (module-level constants); on overflow they evict the least-recently-used entry via `OrderedDict.popitem(last=False)`. No public API change, no migration, no env-var config.

**Tech Stack:** Python 3.11+, asyncio, starlette, pytest, pytest-anyio, ruff, mypy.

**Spec:** [`docs/superpowers/specs/2026-04-24-middleware-oauth-memory-leak-design.md`](../specs/2026-04-24-middleware-oauth-memory-leak-design.md)

**Branch:** `fix/middleware-oauth-memory-leaks` (already created off `main` at `da6f9bf`).

---

## File Structure

| File | Status | Responsibility |
|------|--------|----------------|
| `src/mcp_awareness/middleware.py` | Modify | Replace semaphore-based concurrency limit with bounded counter |
| `src/mcp_awareness/oauth_proxy.py` | Modify | Add LRU cap to `RateLimiter._hits` |
| `tests/test_middleware.py` | Modify | Add 5 tests for bounded counter behavior |
| `tests/test_oauth_proxy.py` | Modify | Add 3 tests for `_hits` LRU bounding |
| `CHANGELOG.md` | Modify | `[Unreleased] / Fixed` entry |

---

## Task 1: Bound `AuthMiddleware` concurrency tracking

**Files:**
- Modify: `src/mcp_awareness/middleware.py:21-22` (imports), `:207` (state init), `:250-268` (acquire/release block)
- Modify: `tests/test_middleware.py` (add tests to `TestAuthMiddleware` class near line 570)

### Step 1.1: Write failing tests

- [ ] Add the following 5 tests to `tests/test_middleware.py` inside `class TestAuthMiddleware:` (anywhere after the existing helpers and before the `TestStreamableHttpWiring` section):

```python
    @pytest.mark.anyio
    async def test_owner_inflight_dict_bounded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """`_owner_inflight` never grows past `_MAX_OWNERS`, even under live in-flight load."""
        # Lower the cap so the test runs fast.
        monkeypatch.setattr("mcp_awareness.middleware._MAX_OWNERS", 3)

        gate = asyncio.Event()

        async def slow_app(scope: dict[str, Any], receive: Any, send: Any) -> None:
            await gate.wait()
            await _dummy_app(scope, receive, send)

        app = AuthMiddleware(slow_app, _JWT_SECRET, max_concurrent_per_owner=10)
        tasks = []
        for i in range(5):  # 3 cap + 2 over
            token = self._make_token(sub=f"owner-{i}")
            scope = {
                "type": "http",
                "path": "/mcp",
                "method": "POST",
                "headers": [(b"authorization", f"Bearer {token}".encode())],
            }
            tasks.append(asyncio.create_task(_collect_response(app, scope)))
        # Yield repeatedly so all tasks reach the gate.
        for _ in range(20):
            await asyncio.sleep(0)
        try:
            assert len(app._owner_inflight) <= 3
        finally:
            gate.set()
            for t in tasks:
                await t

    @pytest.mark.anyio
    async def test_owner_inflight_evicts_lru(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Filling past the cap evicts the least-recently-used owner_id."""
        monkeypatch.setattr("mcp_awareness.middleware._MAX_OWNERS", 3)

        gate = asyncio.Event()

        async def slow_app(scope: dict[str, Any], receive: Any, send: Any) -> None:
            await gate.wait()
            await _dummy_app(scope, receive, send)

        app = AuthMiddleware(slow_app, _JWT_SECRET, max_concurrent_per_owner=10)
        tasks = []
        for i in range(4):  # owner-0 first → evicted when owner-3 arrives
            token = self._make_token(sub=f"owner-{i}")
            scope = {
                "type": "http",
                "path": "/mcp",
                "method": "POST",
                "headers": [(b"authorization", f"Bearer {token}".encode())],
            }
            tasks.append(asyncio.create_task(_collect_response(app, scope)))
        for _ in range(20):
            await asyncio.sleep(0)
        try:
            assert "owner-0" not in app._owner_inflight
            assert "owner-3" in app._owner_inflight
            assert len(app._owner_inflight) == 3
        finally:
            gate.set()
            for t in tasks:
                await t

    @pytest.mark.anyio
    async def test_concurrent_limit_returns_429(self) -> None:
        """When inflight count for one owner_id reaches `_max_concurrent`, the next request gets 429."""
        # Build a slow app that blocks on an event so we can pile up inflight requests.
        gate = asyncio.Event()

        async def slow_app(scope: dict[str, Any], receive: Any, send: Any) -> None:
            await gate.wait()
            await _dummy_app(scope, receive, send)

        app = AuthMiddleware(slow_app, _JWT_SECRET, max_concurrent_per_owner=2)
        token = self._make_token(sub="alice")
        scope = {
            "type": "http",
            "path": "/mcp",
            "method": "POST",
            "headers": [(b"authorization", f"Bearer {token}".encode())],
        }

        # Start two concurrent requests — both should be in flight (waiting on gate).
        in_flight = [asyncio.create_task(_collect_response(app, scope)) for _ in range(2)]
        # Yield so the tasks reach the gate.
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        # A third request must be rejected with 429.
        status, body = await _collect_response(app, scope)
        assert status == 429
        data = json.loads(body)
        assert "too many" in data["error"].lower()

        # Release the gate so the in-flight tasks finish.
        gate.set()
        for task in in_flight:
            await task

    @pytest.mark.anyio
    async def test_inflight_collapses_after_completion(self) -> None:
        """After a request completes, the owner's entry is removed from `_owner_inflight`."""
        app = self._make_app()
        token = self._make_token(sub="alice")
        scope = {
            "type": "http",
            "path": "/mcp",
            "method": "POST",
            "headers": [(b"authorization", f"Bearer {token}".encode())],
        }
        status, _ = await _collect_response(app, scope)
        assert status == 200
        assert "alice" not in app._owner_inflight

    @pytest.mark.anyio
    async def test_owner_inflight_thread_safety_under_concurrency(self) -> None:
        """100 concurrent requests for one owner_id with `_max_concurrent=100` all succeed and the counter returns to 0."""

        async def app_inner(scope: dict[str, Any], receive: Any, send: Any) -> None:
            # Yield to the event loop a few times so coroutines interleave.
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            await _dummy_app(scope, receive, send)

        app = AuthMiddleware(app_inner, _JWT_SECRET, max_concurrent_per_owner=100)
        token = self._make_token(sub="alice")
        scope = {
            "type": "http",
            "path": "/mcp",
            "method": "POST",
            "headers": [(b"authorization", f"Bearer {token}".encode())],
        }
        results = await asyncio.gather(
            *[_collect_response(app, scope) for _ in range(100)]
        )
        assert all(status == 200 for status, _ in results)
        assert "alice" not in app._owner_inflight
```

- [ ] At the top of `tests/test_middleware.py`, ensure `import asyncio` is present (it is not currently — add it after `import json` on line 21):

```python
import asyncio
import json
```

### Step 1.2: Run tests, verify they fail

- [ ] Run: `python -m pytest tests/test_middleware.py::TestAuthMiddleware -v -k "inflight or concurrent_limit"`

  Expected: All 5 new tests FAIL with `ImportError: cannot import name '_MAX_OWNERS' from 'mcp_awareness.middleware'` or `AttributeError: 'AuthMiddleware' object has no attribute '_owner_inflight'`.

### Step 1.3: Implement the bounded counter

- [ ] In `src/mcp_awareness/middleware.py`, modify the imports (lines 21-25). Replace:

```python
import asyncio
import logging
import pathlib
from collections.abc import Callable
from typing import Any
```

with:

```python
import asyncio
import logging
import pathlib
from collections import OrderedDict
from collections.abc import Callable
from typing import Any

# Cap on the number of owner_id keys tracked in AuthMiddleware._owner_inflight.
# Bounds memory in long-running deployments; LRU-evicts on overflow.
_MAX_OWNERS = 10_000
```

- [ ] Replace `_owner_semaphores` initialization at line 207. Change:

```python
        self._owner_semaphores: dict[str, asyncio.Semaphore] = {}
```

to:

```python
        self._owner_inflight: OrderedDict[str, int] = OrderedDict()
        self._owner_lock = asyncio.Lock()
```

- [ ] Replace the concurrency-limit block (lines 249-268). Change:

```python
        # Per-owner concurrency limit — prevents one client from DOSing others
        sem = self._owner_semaphores.get(owner_id)
        if sem is None:
            sem = asyncio.Semaphore(self._max_concurrent)
            self._owner_semaphores[owner_id] = sem

        if not sem._value:  # All slots taken — reject immediately
            resp = JSONResponse({"error": "Too many concurrent requests"}, status_code=429)
            await resp(scope, receive, send)
            return

        # Set owner context for downstream handlers
        from .server import _owner_ctx

        async with sem:
            token_reset = _owner_ctx.set(owner_id)
            try:
                await self.app(scope, receive, send)
            finally:
                _owner_ctx.reset(token_reset)
```

to:

```python
        # Per-owner concurrency limit — prevents one client from DOSing others.
        # Counter-based; intrinsically bounded by _MAX_OWNERS via LRU eviction.
        async with self._owner_lock:
            inflight = self._owner_inflight.get(owner_id, 0)
            over_limit = inflight >= self._max_concurrent
            if not over_limit:
                self._owner_inflight[owner_id] = inflight + 1
                self._owner_inflight.move_to_end(owner_id)
                if len(self._owner_inflight) > _MAX_OWNERS:
                    self._owner_inflight.popitem(last=False)

        if over_limit:
            resp = JSONResponse({"error": "Too many concurrent requests"}, status_code=429)
            await resp(scope, receive, send)
            return

        # Set owner context for downstream handlers
        from .server import _owner_ctx

        token_reset = _owner_ctx.set(owner_id)
        try:
            await self.app(scope, receive, send)
        finally:
            _owner_ctx.reset(token_reset)
            async with self._owner_lock:
                new_count = self._owner_inflight.get(owner_id, 1) - 1
                if new_count <= 0:
                    self._owner_inflight.pop(owner_id, None)
                else:
                    self._owner_inflight[owner_id] = new_count
```

### Step 1.4: Run tests, verify they pass

- [ ] Run: `python -m pytest tests/test_middleware.py::TestAuthMiddleware -v`

  Expected: All `TestAuthMiddleware` tests PASS (existing + 5 new).

- [ ] Run: `python -m pytest tests/test_middleware.py -v`

  Expected: Full test_middleware.py file passes.

### Step 1.5: Commit

- [ ] Run:

```bash
git add src/mcp_awareness/middleware.py tests/test_middleware.py
git commit -m "$(cat <<'EOF'
fix(middleware): bound AuthMiddleware per-owner concurrency tracking

Replace `dict[str, asyncio.Semaphore]` with `OrderedDict[str, int]`
guarded by an `asyncio.Lock`, capped at 10,000 owner_ids with LRU
eviction. Removes the `asyncio.Semaphore._value` private-attr read
that was used to detect "full" semaphores. Counter is intrinsically
race-free under the lock and steady-state size collapses to active
owner count.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: Bound `RateLimiter._hits` with LRU cap

**Files:**
- Modify: `src/mcp_awareness/oauth_proxy.py` (imports near line 30, `RateLimiter.__init__` line 138, `RateLimiter.check` lines 144-168)
- Modify: `tests/test_oauth_proxy.py` (add tests to `TestRateLimiter` class)

### Step 2.1: Write failing tests

- [ ] Add the following 3 tests to `tests/test_oauth_proxy.py` inside `class TestRateLimiter:` (after `test_stats` near line 128):

```python
    def test_hits_dict_bounded(self) -> None:
        """`_hits` never grows past `_MAX_RATE_LIMIT_IPS`."""
        from mcp_awareness.oauth_proxy import _MAX_RATE_LIMIT_IPS

        rl = RateLimiter(max_requests=10, window_seconds=60)
        for i in range(_MAX_RATE_LIMIT_IPS + 1):
            rl.check(f"10.0.{i // 256}.{i % 256}")
        assert len(rl._hits) <= _MAX_RATE_LIMIT_IPS

    def test_hits_evicts_lru_ip(self) -> None:
        """Filling past the cap evicts the oldest IP."""
        from mcp_awareness.oauth_proxy import _MAX_RATE_LIMIT_IPS

        rl = RateLimiter(max_requests=10, window_seconds=60)
        first_ip = "10.0.0.0"
        for i in range(_MAX_RATE_LIMIT_IPS):
            rl.check(f"10.0.{i // 256}.{i % 256}")
        assert first_ip in rl._hits
        # One more IP — should evict the LRU (first_ip).
        rl.check("192.168.1.1")
        assert first_ip not in rl._hits
        assert "192.168.1.1" in rl._hits
        assert len(rl._hits) == _MAX_RATE_LIMIT_IPS

    def test_hits_recency_updated_on_access(self) -> None:
        """Re-hitting an existing IP refreshes its LRU position."""
        from mcp_awareness.oauth_proxy import _MAX_RATE_LIMIT_IPS

        rl = RateLimiter(max_requests=10, window_seconds=60)
        # Fill to cap.
        for i in range(_MAX_RATE_LIMIT_IPS):
            rl.check(f"10.0.{i // 256}.{i % 256}")
        # Re-hit the oldest IP — moves it to the end.
        rl.check("10.0.0.0")
        # Now add a new IP; the second-oldest (10.0.0.1) should be evicted, not 10.0.0.0.
        rl.check("192.168.1.1")
        assert "10.0.0.0" in rl._hits
        assert "10.0.0.1" not in rl._hits
        assert "192.168.1.1" in rl._hits
```

### Step 2.2: Run tests, verify they fail

- [ ] Run: `python -m pytest tests/test_oauth_proxy.py::TestRateLimiter -v -k "hits_dict_bounded or evicts_lru or recency"`

  Expected: All 3 new tests FAIL with `ImportError: cannot import name '_MAX_RATE_LIMIT_IPS'`.

### Step 2.3: Implement LRU cap

- [ ] In `src/mcp_awareness/oauth_proxy.py`, add the import near line 30 (after `import time` at line 34). Replace the import block:

```python
import asyncio
import json
import logging
import re
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qs
```

with:

```python
import asyncio
import json
import logging
import re
import time
import urllib.error
import urllib.request
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qs

# Cap on the number of IP keys tracked in RateLimiter._hits.
# Bounds memory in long-running deployments; LRU-evicts on overflow.
_MAX_RATE_LIMIT_IPS = 10_000
```

- [ ] In `RateLimiter.__init__`, replace line 138:

```python
        self._hits: dict[str, list[float]] = {}
```

with:

```python
        self._hits: OrderedDict[str, list[float]] = OrderedDict()
```

- [ ] In `RateLimiter.check`, replace the body from line 156 onward. Change:

```python
        # Sliding window
        timestamps = self._hits.get(ip, [])
        cutoff = now - self.window_seconds
        timestamps = [t for t in timestamps if t > cutoff]

        if len(timestamps) >= self.max_requests:
            self._hits[ip] = timestamps
            self._rate_limited_count += 1
            self._last_rate_limited = datetime.now(timezone.utc).isoformat()
            return False

        timestamps.append(now)
        self._hits[ip] = timestamps
        return True
```

to:

```python
        # Sliding window
        timestamps = self._hits.get(ip, [])
        cutoff = now - self.window_seconds
        timestamps = [t for t in timestamps if t > cutoff]

        if len(timestamps) >= self.max_requests:
            self._hits[ip] = timestamps
            self._hits.move_to_end(ip)
            self._rate_limited_count += 1
            self._last_rate_limited = datetime.now(timezone.utc).isoformat()
            return False

        timestamps.append(now)
        self._hits[ip] = timestamps
        self._hits.move_to_end(ip)
        if len(self._hits) > _MAX_RATE_LIMIT_IPS:
            self._hits.popitem(last=False)
        return True
```

### Step 2.4: Run tests, verify they pass

- [ ] Run: `python -m pytest tests/test_oauth_proxy.py::TestRateLimiter -v`

  Expected: All `TestRateLimiter` tests PASS (existing + 3 new).

- [ ] Run: `python -m pytest tests/test_oauth_proxy.py -v`

  Expected: Full test_oauth_proxy.py file passes.

### Step 2.5: Commit

- [ ] Run:

```bash
git add src/mcp_awareness/oauth_proxy.py tests/test_oauth_proxy.py
git commit -m "$(cat <<'EOF'
fix(oauth_proxy): bound RateLimiter._hits with LRU cap

Switch `_hits` to `OrderedDict` and cap at 10,000 IPs with
`popitem(last=False)` LRU eviction. Each `check()` calls
`move_to_end` so active IPs stay at the end. `_bans` is unchanged —
it self-heals on access and is bounded by ban_duration × ban_rate.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: Update CHANGELOG

**Files:**
- Modify: `CHANGELOG.md` (under `## [Unreleased]` → `### Fixed`)

### Step 3.1: Add CHANGELOG entry

- [ ] In `CHANGELOG.md`, the current `## [Unreleased]` block has only a `### Security` subsection. Add a `### Fixed` subsection above the `### Security` subsection (so it precedes it within `[Unreleased]`).

  The exact text to insert immediately after the `## [Unreleased]` line and the blank line that follows:

```markdown
### Fixed
- **Bound `AuthMiddleware._owner_inflight` and `RateLimiter._hits` to prevent unbounded growth in long-running deployments.** `AuthMiddleware` previously kept one `asyncio.Semaphore` per distinct `owner_id` in a dict that was never pruned; `RateLimiter` kept one entry per distinct client IP in `_hits` (timestamps inside each entry were pruned, but the outer dict grew forever). Both are now `OrderedDict`s capped at 10,000 entries with LRU eviction via `popitem(last=False)`. The semaphore design is replaced with a counter-based `OrderedDict[str, int]` guarded by an `asyncio.Lock`, removing the `asyncio.Semaphore._value` private-attribute read previously used to detect a full semaphore. Steady-state `_owner_inflight` size now collapses to currently-active owners rather than growing with lifetime distinct owners. No public API change, no migration, no env-var config; tests cover bounded growth, LRU eviction, recency refresh, 429 behavior under concurrency, and counter collapse on completion.
```

### Step 3.2: Verify markdown still parses

- [ ] Run: `head -20 CHANGELOG.md`

  Expected: `## [Unreleased]` followed by blank line, then `### Fixed` block, then `### Security` block.

### Step 3.3: Commit

- [ ] Run:

```bash
git add CHANGELOG.md
git commit -m "$(cat <<'EOF'
docs(changelog): note middleware + oauth_proxy memory-leak fixes

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: Pre-push verification

**Files:** none — verification only.

### Step 4.1: Lint

- [ ] Run: `ruff check src/ tests/`

  Expected: No errors.

- [ ] Run: `ruff format --check src/ tests/`

  Expected: No errors. If formatting drift is reported, run `ruff format src/ tests/` and amend the prior commit:

  ```bash
  ruff format src/ tests/
  git add src/ tests/
  git commit --amend --no-edit
  ```

### Step 4.2: Type check

- [ ] Run: `mypy src/mcp_awareness/`

  Expected: `Success: no issues found in N source files`.

### Step 4.3: Full test suite

- [ ] Run: `python -m pytest tests/ -x --tb=short`

  Expected: All tests pass. Note the new test count and confirm it is exactly 8 higher than the prior count on `main` (5 in test_middleware.py + 3 in test_oauth_proxy.py).

### Step 4.4: Coverage spot-check on changed files

- [ ] Run:

```bash
python -m pytest tests/test_middleware.py tests/test_oauth_proxy.py --cov=src/mcp_awareness/middleware --cov=src/mcp_awareness/oauth_proxy --cov-report=term-missing
```

  Expected: coverage on the changed lines is 100% (both the acquire and release paths in `middleware.py`, and the new `move_to_end`/`popitem` lines in `oauth_proxy.py`). If any new line is reported as missed, add a test that exercises it before pushing.

---

## Task 5: Push and open PR

**Files:** none — git/gh operations only.

### Step 5.1: Push branch

- [ ] Run:

```bash
git push -u origin fix/middleware-oauth-memory-leaks
```

### Step 5.2: Apply Dev Active label and open PR

- [ ] Run:

```bash
gh pr create --title "fix(sec): bound AuthMiddleware concurrency dict and RateLimiter hits dict" --body "$(cat <<'EOF'
## Summary
- `AuthMiddleware`: replace per-owner `dict[str, asyncio.Semaphore]` with `OrderedDict[str, int]` of in-flight counts under an `asyncio.Lock`, capped at 10,000 owner_ids with LRU eviction. Removes `asyncio.Semaphore._value` private-attr read.
- `RateLimiter`: switch `_hits` to `OrderedDict[str, list[float]]` with the same 10,000-entry LRU cap. `_bans` is left as-is (self-healing).

Three findings from prior code reviews, single PR. Spec: `docs/superpowers/specs/2026-04-24-middleware-oauth-memory-leak-design.md`. Targets v0.18.4.

## Test plan
- [ ] `python -m pytest tests/test_middleware.py tests/test_oauth_proxy.py -v`
- [ ] `ruff check src/ tests/ && ruff format --check src/ tests/`
- [ ] `mypy src/mcp_awareness/`
- [ ] Full suite: `python -m pytest tests/`

## QA

### Prerequisites
- `pip install -e ".[dev]"`
- Deploy to test instance on alternate port (`AWARENESS_PORT=8421`) — only needed if validating live behavior; unit tests cover the change end-to-end.

### Manual tests (via MCP tools)
1. - [ ] **Auth still works after the change** — connect any MCP client; expect tools to be reachable as before.
2. - [ ] **429 path** — saturate `max_concurrent_per_owner` (default 3) for one owner via three concurrent slow tool calls; the 4th must return HTTP 429 with `{"error": "Too many concurrent requests"}`.
3. - [ ] **Counter collapses** — after the saturation test completes, restart-free, fire one more request; expect 200, and `_owner_inflight` (visible via debugger or stats endpoint if added) should be empty for that owner_id between requests.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

### Step 5.3: Apply lifecycle labels

- [ ] Run:

```bash
gh pr edit --add-label "Dev Active" "$(gh pr view --json number -q .number)"
```

### Step 5.4: Wait for CI

- [ ] Run: `gh pr checks --watch`

  Expected: All required checks pass. If any fails, fix locally, commit, push, and re-run.

### Step 5.5: Read Codecov bot comment

- [ ] Run: `gh pr view --comments | grep -A 30 "codecov"`

  Expected: Codecov reports patch coverage. If any new line is uncovered, add a test, push, and rerun checks.

### Step 5.6: Transition to Ready for QA

- [ ] Run:

```bash
PR_NUM=$(gh pr view --json number -q .number)
gh pr edit --remove-label "Dev Active" --add-label "Ready for QA" "$PR_NUM"
```

### Step 5.7: Notify QA via awareness

- [ ] Use the awareness MCP tool `add_context` to write a `pr-notify` entry:

```
add_context(
  source="claude-dev",
  tags=["pr-notify", "mcp-awareness"],
  description="PR #<num>: ready-for-qa — fix(sec): bound AuthMiddleware concurrency dict and RateLimiter hits dict | branch: fix/middleware-oauth-memory-leaks | summary: Caps both dicts at 10k via OrderedDict LRU eviction; removes asyncio.Semaphore._value private-attr read",
  expires_days=1,
)
```

---

## Self-Review Notes

**Spec coverage check:**
- Fix 1 (`_owner_semaphores` unbounded) → Task 1 (state init + acquire/release block, 5 tests)
- Fix 1 (private-attr `_value` read) → Task 1 (replaced with counter design, no `_value` left)
- Fix 2 (`_hits` unbounded) → Task 2 (OrderedDict + LRU cap + 3 tests)
- Caps hardcoded at 10,000 → Task 1 (`_MAX_OWNERS`) + Task 2 (`_MAX_RATE_LIMIT_IPS`)
- CHANGELOG `[Unreleased]` → Task 3
- Targets v0.18.4 → noted in PR body; release PR is a separate follow-up per project release process

**No placeholders:** every step has actual code or actual command output expectations.

**Type / name consistency:** `_owner_inflight`, `_owner_lock`, `_MAX_OWNERS`, `_MAX_RATE_LIMIT_IPS` are referenced consistently across the plan and the spec.

**Out of scope per spec:** `_bans` eviction, env-var configurability of caps, geo/ASN tagging, async conversion of `RateLimiter`. None of these appear in any task.
