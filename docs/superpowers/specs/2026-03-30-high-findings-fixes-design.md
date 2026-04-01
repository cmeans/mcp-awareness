<!-- SPDX-License-Identifier: AGPL-3.0-or-later | Copyright (C) 2026 Chris Means -->
# High Findings Fixes — Design Spec

**Date:** 2026-03-30
**Status:** Approved
**Scope:** Fix all 5 HIGH findings from the 2026-03-30 deep audit

## Background

A deep code audit on 2026-03-30 identified 5 HIGH-severity findings across three areas: store data integrity (3), tool input validation (1), and collator query performance (1). PRs #98-102 addressed other audit findings but these 5 remain unresolved.

## Delivery Plan

Three PRs, merged sequentially:

1. **PR: Store safety** — authorization + race conditions
2. **PR: Date validation** — structured errors for malformed dates
3. **PR: Collator batch queries** — eliminate N+1 pattern

---

## PR 1: Store Safety

### Finding 1.1 — `update_intention_state` missing owner_id

**File:** `postgres_store.py`, `sql/update_intention_state.sql`

The UPDATE statement uses `WHERE id = %s` without `AND owner_id = %s`. A user who knows an entry ID can update another user's intention. RLS is defense-in-depth but application-layer enforcement must be consistent with all other UPDATE statements.

**Fix:**
- Add `AND owner_id = %s` to `sql/update_intention_state.sql`
- Pass `owner_id` parameter in `postgres_store.py`

**Tests:**
- Attempt to update intention with wrong owner_id, assert no rows affected
- Verify correct owner can still update

### Finding 1.2 — `upsert_alert` race condition

**File:** `postgres_store.py`

Current pattern: `_query_entries()` (connection 1) then UPDATE/INSERT (connection 2). Between the two, a concurrent request can create duplicates or lose updates.

**Fix:** Refactor to single connection:
1. Open connection + transaction
2. `SELECT ... FOR UPDATE` to find existing entry by source + alert_id
3. UPDATE if found, INSERT if not — all within the same transaction

**Tests:**
- Upsert same alert_id twice, assert single entry with merged data
- Upsert different alert_ids, assert two entries

### Finding 1.3 — `upsert_preference` race condition

**File:** `postgres_store.py`

Identical pattern to upsert_alert. Read in one connection, write in another.

**Fix:** Same approach — single connection with `SELECT ... FOR UPDATE` by key + scope.

**Tests:**
- Upsert same key+scope twice, assert single entry with merged data
- Upsert different keys, assert two entries

---

## PR 2: Date Validation

### Finding 2.1 — Unhandled ValueError on malformed dates

**File:** `tools.py`

All 9+ call sites using `ensure_dt()` or `parse_iso()` lack try/except. Malformed date strings crash the tool with a Python stack trace instead of structured JSON. Violates the documented MCP contract ("This tool always returns structured JSON").

**Fix:** Add a `_safe_ensure_dt(val: str | datetime) -> tuple[datetime | None, str | None]` helper that wraps `ensure_dt()` in try/except ValueError. Follows the existing `_parse_entry_type()` pattern.

Each call site becomes:
```python
since_dt, err = _safe_ensure_dt(since)
if err:
    return json.dumps({"status": "error", "message": err})
```

**Call sites (~9):**
- `get_alerts`: since
- `get_knowledge`: since, until, created_after, created_before
- `get_deleted`: since
- `get_reads`: since
- `get_actions`: since
- `get_unread`: since
- `get_activity`: since
- `remind`: deliver_at
- `semantic_search`: since, until

**Tests:**
- Malformed date string returns structured JSON error (not stack trace)
- Valid ISO dates still work as before
- Edge cases: empty string (already handled separately), "Z" suffix, naive datetimes

---

## PR 3: Collator Batch Queries

### Finding 3.1 — N+1 query pattern in generate_briefing

**File:** `collator.py`, `postgres_store.py`, `store.py`

Current: `generate_briefing()` loops over N sources, executing 4 queries per source (status, alerts, suppressions, patterns). 20 sources = 80 DB round trips.

**Fix:** Add 4 batch methods to the Store protocol and PostgresStore, then refactor `generate_briefing()` to call them once upfront.

### New Store Protocol Methods

```python
def get_all_statuses(self, owner_id: str) -> dict[str, Entry]:
    """Get latest status for every source. Returns {source: Entry}."""

def get_all_active_alerts(self, owner_id: str) -> dict[str, list[Entry]]:
    """Get all active alerts grouped by source. Returns {source: [Entry]}."""

def get_all_active_suppressions(self, owner_id: str) -> dict[str, list[Entry]]:
    """Get all active suppressions grouped by source. Includes global (empty source)."""

def get_all_patterns(self, owner_id: str) -> dict[str, list[Entry]]:
    """Get all patterns grouped by source. Includes global (empty source)."""
```

### New SQL Files

One per batch method. Variations of existing per-source queries with `source = %s` filter removed. Grouping done in Python via `defaultdict`.

### Refactored `generate_briefing()`

```
1. sources = store.get_sources(owner_id)
2. all_statuses = store.get_all_statuses(owner_id)
3. all_alerts = store.get_all_active_alerts(owner_id)
4. all_suppressions = store.get_all_active_suppressions(owner_id)
5. all_patterns = store.get_all_patterns(owner_id)
6. for source in sources:
       status = all_statuses.get(source)
       alerts = all_alerts.get(source, [])
       suppressions = all_suppressions.get(source, []) + all_suppressions.get("", [])
       patterns = all_patterns.get(source, []) + all_patterns.get("", [])
       # ... same logic as before
```

Total: 5 queries fixed, regardless of source count.

### Existing per-source methods

Kept as-is — they're used by individual tools outside of briefing generation.

**Tests:**
- Verify briefing output matches before/after (regression)
- Batch methods return correct grouping with multiple sources
- Empty source (global) suppressions and patterns included correctly

---

## Out of Scope

- MEDIUM and LOW findings from the same audit (separate future work)
- Token efficiency improvements
- Error response schema standardization (beyond date errors)
- Collator behavior changes (OK source omission, intention state transitions)

## Risk

- **PR 1**: Low risk. SQL changes are minimal. `SELECT FOR UPDATE` is standard Postgres.
- **PR 2**: Low risk. Mechanical wrapping of existing calls.
- **PR 3**: Medium risk. New protocol methods and refactored briefing loop. Regression testing is critical.
