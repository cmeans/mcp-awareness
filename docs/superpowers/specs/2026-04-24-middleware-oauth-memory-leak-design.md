<!-- SPDX-License-Identifier: AGPL-3.0-or-later | Copyright (C) 2026 Chris Means -->
# Middleware + OAuth Memory-Leak Cluster — Design Spec

**Date:** 2026-04-24
**Status:** Approved
**Scope:** Bound three unbounded data structures and remove a private-attr access pattern in the auth middleware and OAuth proxy. Single PR, ships under `[Unreleased]` → v0.18.4.

## Background

Three findings from prior code reviews remain unresolved on `main` (`da6f9bf`):

1. `src/mcp_awareness/middleware.py:207` — `_owner_semaphores: dict[str, asyncio.Semaphore]` grows unbounded; one entry per distinct `owner_id` (intention `83949b31-beb6-4273-9043-15812c397d30`, dup `bb48062d-643d-4259-8db0-f8659b565c05`).
2. `src/mcp_awareness/middleware.py:255` — `if not sem._value:` reads a private attribute of `asyncio.Semaphore`. The check is correct under single-threaded asyncio (no `await` between check and acquire) but the API coupling is brittle (intention `a91862d6-2469-4962-957f-d10e6c46a6c1`).
3. `src/mcp_awareness/oauth_proxy.py:138` — `RateLimiter._hits: dict[str, list[float]]` keys (per-IP) are never evicted. Inner timestamp lists are pruned per-IP, but the outer dict grows for every IP that has ever called the proxy. `_bans` self-heals on access; `_hits` does not (intention `fb15d74c-d63c-4b2a-9a0d-6a4add0cb51c`).

Long-running deployments behind public OAuth endpoints accrete entries indefinitely. This is a slow leak, not an active vulnerability — the goal is to bound it.

## Approach

Replace per-owner semaphores with a counter-based design that is intrinsically race-free, avoids private attrs, and is easy to bound. Wrap the rate-limiter `_hits` map in an `OrderedDict` with a hard cap and opportunistic empty-entry eviction.

Caps are hardcoded constants (10,000 each); no env vars. YAGNI — operators can tune later if a deployment needs to.

---

## Fix 1 — Replace `_owner_semaphores` with a bounded counter dict

**File:** `src/mcp_awareness/middleware.py`

Drop the `dict[str, asyncio.Semaphore]` and the `_value` private-attr read. Use an `OrderedDict[str, int]` of in-flight counts, guarded by a single `asyncio.Lock`. The lock is held only across the synchronous read-check-update path (no awaits inside), so it does not introduce a bottleneck.

Constants:
- `_MAX_OWNERS = 10_000` (module-level)

State:
- `self._owner_inflight: OrderedDict[str, int] = OrderedDict()`
- `self._owner_lock = asyncio.Lock()`

Acquire path (replaces lines 250-258):
```python
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
```

Release path (replaces `async with sem:` block):
```python
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

Notes:
- The `OrderedDict.popitem(last=False)` evicts the least-recently-used owner_id. An evicted owner with non-zero inflight count drops their counter to "fresh" — worst case they get one extra concurrent slot. Acceptable: the cap protects the process; a malicious actor cannot benefit because they would have to push 10k other owners through to evict their own counter, which already implies legitimate distinct-owner traffic dominates them.
- The release-path `pop` collapses zero-count entries, so steady-state size = number of currently-active owners, not lifetime distinct owners.

**Tests** (`tests/test_middleware.py`):
- `test_owner_inflight_dict_bounded` — fan in 10,001 distinct owner_ids serially, assert `len(self._owner_inflight) <= 10_000`.
- `test_owner_inflight_evicts_lru` — fill cap, add one more, assert oldest owner_id is gone.
- `test_concurrent_limit_returns_429` — saturate one owner_id with `_max_concurrent` in-flight requests (mock app blocks on event), 4th request gets 429. Use `asyncio.gather` with manual event control.
- `test_inflight_collapses_after_completion` — single owner_id makes N requests serially, assert after each completes the dict has no entry for that owner_id.
- `test_owner_inflight_thread_safety_under_concurrency` — fire 100 concurrent requests for one owner_id (with `_max_concurrent=100`, mocked app), assert all succeed and final count is 0.

---

## Fix 2 — Bound `RateLimiter._hits` with an `OrderedDict` + LRU cap

**File:** `src/mcp_awareness/oauth_proxy.py`

Replace `self._hits: dict[str, list[float]]` with `OrderedDict[str, list[float]]`. Add hard cap; on every `check()` move the IP to the end (LRU recency); when an IP's timestamps list becomes empty after the window cutoff, delete the entry.

Constants:
- `_MAX_RATE_LIMIT_IPS = 10_000` (module-level)

`check()` modifications:
- When writing/refreshing an IP's entry: assign then `self._hits.move_to_end(ip)` so most-recent IP is at the end.
- After insert (the "allowed" branch): if `len(self._hits) > _MAX_RATE_LIMIT_IPS` → `self._hits.popitem(last=False)` (evict LRU).

Pseudocode for the modified middle of `check()`:
```python
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

Notes:
- Entries written to `_hits` are always non-empty (we just appended `now` in the allowed branch, or the rate-limit branch only fires when `len(timestamps) >= max_requests > 0`). No need for an explicit "delete empty entries" pass — silent IPs get evicted by LRU pressure when the cap fills.
- Eviction loses an evicted IP's recent-hit history; they get a fresh window on return. Same trade as Fix 1: the cap exists to bound memory, not to enforce strict per-IP fairness at extreme cardinality.
- `_bans` is left as-is — it self-heals on access and bans are short-lived (1h default), so growth is naturally bounded by `ban_duration × ban_rate`.

**Tests** (`tests/test_oauth_proxy.py`, `TestRateLimiter`):
- `test_hits_dict_bounded` — fire one hit each from 10,001 distinct IPs, assert `len(rl._hits) <= 10_000`.
- `test_hits_evicts_lru_ip` — fill cap with IPs `A1..A10000`, then hit `B` → assert `A1` is gone, `B` is present.
- `test_hits_recency_updated_on_access` — fill cap with `A1..A10000`, re-hit `A1` (which moves it to end), then hit `B` → assert `A1` still present, `A2` evicted.

---

## Out of Scope

- `_bans` eviction (self-heals, bounded by ban duration × rate)
- Configurable cap sizes via env vars
- Per-IP geo/ASN tagging (separate concern)
- Conversion of `RateLimiter` to async (currently sync; not relevant to this fix)

## Risk

Low. Changes are local to two files. The semaphore replacement preserves observable behavior (concurrent-limit enforcement + 429 response). The rate-limiter changes preserve behavior under normal load and only diverge under cardinality pressure (>10k unique IPs in window) where the previous code would leak unboundedly.

## Compatibility

- No public API change.
- No DB migration.
- No client-facing protocol change.
- No env var change.

## Release

- CHANGELOG entry under `[Unreleased]` → `### Fixed`: "Bound `_owner_semaphores` and `RateLimiter._hits` to prevent unbounded growth in long-running deployments. Replace `_owner_semaphores` semaphore dict with a counter-based design that avoids reading `asyncio.Semaphore._value`."
- Targets v0.18.4 patch release (separate release PR per project convention).
