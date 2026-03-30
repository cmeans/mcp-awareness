# High Findings Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix all 5 HIGH-severity findings from the 2026-03-30 deep audit across three sequential PRs.

**Architecture:** Three independent PRs targeting store safety (postgres_store.py + SQL), date validation (tools.py), and collator batch queries (store.py + postgres_store.py + collator.py). Each PR is self-contained with its own branch, tests, changelog entry, and QA section.

**Tech Stack:** Python 3.10+, psycopg 3.x, PostgreSQL, pytest, testcontainers

---

## PR 1: Store Safety

Branch: `fix/store-safety`

### Task 1: Fix update_intention_state missing owner_id

**Files:**
- Modify: `src/mcp_awareness/sql/update_intention_state.sql`
- Modify: `src/mcp_awareness/postgres_store.py:1025-1030`
- Test: `tests/test_store.py`

- [ ] **Step 1: Write failing test — wrong owner cannot update intention**

Add to `tests/test_store.py` after the existing `test_update_intention_state_wrong_type` test (~line 1370):

```python
def test_update_intention_state_wrong_owner(store):
    """update_intention_state rejects updates from a different owner."""
    now = now_utc()
    entry = Entry(
        id=make_id(),
        type=EntryType.INTENTION,
        source="test",
        tags=[],
        created=now,
        expires=None,
        data={"goal": "test goal", "state": "pending"},
    )
    store.add(TEST_OWNER, entry)
    result = store.update_intention_state("wrong-owner", entry.id, "fired")
    assert result is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_store.py::test_update_intention_state_wrong_owner -v`
Expected: FAIL — the update succeeds because owner_id is not checked in the SQL

- [ ] **Step 3: Fix the SQL — add owner_id to WHERE clause**

Update `src/mcp_awareness/sql/update_intention_state.sql`:

```sql
/* name: update_intention_state */
/* mode: literal */
/* Update an intention entry's data (including state and changelog) and timestamp.
   Python-side computes the state transition and changelog before calling this.
   Params: updated, data (jsonb), id, owner_id
*/
UPDATE entries SET updated = %s, data = %s::jsonb WHERE id = %s AND owner_id = %s
```

- [ ] **Step 4: Pass owner_id in the Python call**

In `src/mcp_awareness/postgres_store.py`, update the `cur.execute` call at lines 1027-1030:

```python
            cur.execute(
                _load_sql("update_intention_state"),
                (now, json.dumps(entry.data), entry.id, owner_id),
            )
```

- [ ] **Step 5: Run test to verify it passes**

Run: `python -m pytest tests/test_store.py::test_update_intention_state_wrong_owner tests/test_store.py::test_update_intention_state -v`
Expected: Both PASS

- [ ] **Step 6: Commit**

```bash
git add src/mcp_awareness/sql/update_intention_state.sql src/mcp_awareness/postgres_store.py tests/test_store.py
git commit -m "fix: add owner_id check to update_intention_state SQL"
```

### Task 2: Fix upsert_alert race condition

**Files:**
- Modify: `src/mcp_awareness/postgres_store.py:253-288`
- Create: `src/mcp_awareness/sql/select_alert_for_update.sql`
- Test: `tests/test_store.py`

- [ ] **Step 1: Write failing test — concurrent upserts must not create duplicates**

Add to `tests/test_store.py`:

```python
def test_upsert_alert_concurrent_no_duplicates(store):
    """Concurrent upsert_alert calls for the same alert_id must not create duplicates."""
    import concurrent.futures

    def do_upsert(i):
        store.upsert_alert(
            TEST_OWNER,
            "nas",
            ["infra"],
            "cpu-race",
            {
                "alert_id": "cpu-race",
                "level": "warning",
                "message": f"CPU high (attempt {i})",
                "resolved": False,
            },
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        futures = [executor.submit(do_upsert, i) for i in range(4)]
        concurrent.futures.wait(futures)
        for f in futures:
            f.result()  # raise any exceptions

    alerts = store.get_active_alerts(TEST_OWNER, source="nas")
    cpu_race = [a for a in alerts if a.data.get("alert_id") == "cpu-race"]
    assert len(cpu_race) == 1, f"Expected 1 alert but found {len(cpu_race)}"
```

- [ ] **Step 2: Run test to verify it can fail under contention**

Run: `python -m pytest tests/test_store.py::test_upsert_alert_concurrent_no_duplicates -v`
Expected: May PASS or FAIL depending on timing — the race is probabilistic. The fix is needed regardless.

- [ ] **Step 3: Create the SELECT FOR UPDATE SQL file**

Create `src/mcp_awareness/sql/select_alert_for_update.sql`:

```sql
/* name: select_alert_for_update */
/* mode: literal */
/* Lock an existing alert row for atomic upsert.
   Params: owner_id, type, source, alert_id
*/
SELECT * FROM entries
WHERE owner_id = %s AND type = %s AND source = %s AND data->>'alert_id' = %s AND deleted IS NULL
ORDER BY COALESCE(updated, created) DESC
FOR UPDATE
```

- [ ] **Step 4: Rewrite upsert_alert to use single connection**

Replace the `upsert_alert` method in `src/mcp_awareness/postgres_store.py` (lines 253-288):

```python
    def upsert_alert(
        self, owner_id: str, source: str, tags: list[str], alert_id: str, data: dict[str, Any]
    ) -> Entry:
        """Upsert an alert by source + alert_id."""
        self._cleanup_expired()
        now = now_utc()
        with self._pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
            self._set_rls_context(cur, owner_id)
            cur.execute(
                _load_sql("select_alert_for_update"),
                (owner_id, EntryType.ALERT.value, source, alert_id),
            )
            row = cur.fetchone()
            if row:
                e = self._row_to_entry(row)
                e.updated = now
                e.tags = tags
                e.data.update(data)
                cur.execute(
                    _load_sql("upsert_alert_update"),
                    (now, json.dumps(e.tags), json.dumps(e.data), e.id, owner_id),
                )
                return e
            entry = Entry(
                id=make_id(),
                type=EntryType.ALERT,
                source=source,
                tags=tags,
                created=now,
                expires=None,
                data=data,
            )
            self._insert_entry(cur, owner_id, entry)
        return entry
```

- [ ] **Step 5: Run tests to verify**

Run: `python -m pytest tests/test_store.py::test_upsert_alert_new tests/test_store.py::test_upsert_alert_updates tests/test_store.py::test_upsert_alert_concurrent_no_duplicates tests/test_store.py::test_upsert_alert_different_sources_same_alert_id -v`
Expected: All PASS

- [ ] **Step 6: Commit**

```bash
git add src/mcp_awareness/postgres_store.py src/mcp_awareness/sql/select_alert_for_update.sql tests/test_store.py
git commit -m "fix: eliminate upsert_alert race condition with SELECT FOR UPDATE"
```

### Task 3: Fix upsert_preference race condition

**Files:**
- Modify: `src/mcp_awareness/postgres_store.py:290-325`
- Create: `src/mcp_awareness/sql/select_preference_for_update.sql`
- Test: `tests/test_store.py`

- [ ] **Step 1: Write failing test — concurrent upserts must not create duplicates**

Add to `tests/test_store.py`:

```python
def test_upsert_preference_concurrent_no_duplicates(store):
    """Concurrent upsert_preference calls for the same key+scope must not create duplicates."""
    import concurrent.futures

    def do_upsert(i):
        store.upsert_preference(
            TEST_OWNER,
            "theme",
            "global",
            ["ui"],
            {"key": "theme", "scope": "global", "value": f"dark-{i}"},
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        futures = [executor.submit(do_upsert, i) for i in range(4)]
        concurrent.futures.wait(futures)
        for f in futures:
            f.result()

    prefs = store.get_knowledge(TEST_OWNER, entry_type=EntryType.PREFERENCE)
    theme_prefs = [p for p in prefs if p.data.get("key") == "theme"]
    assert len(theme_prefs) == 1, f"Expected 1 preference but found {len(theme_prefs)}"
```

- [ ] **Step 2: Run test to verify it can fail under contention**

Run: `python -m pytest tests/test_store.py::test_upsert_preference_concurrent_no_duplicates -v`
Expected: May PASS or FAIL depending on timing.

- [ ] **Step 3: Create the SELECT FOR UPDATE SQL file**

Create `src/mcp_awareness/sql/select_preference_for_update.sql`:

```sql
/* name: select_preference_for_update */
/* mode: literal */
/* Lock an existing preference row for atomic upsert.
   Params: owner_id, type, key, scope
*/
SELECT * FROM entries
WHERE owner_id = %s AND type = %s AND data->>'key' = %s AND data->>'scope' = %s AND deleted IS NULL
ORDER BY COALESCE(updated, created) DESC
FOR UPDATE
```

- [ ] **Step 4: Rewrite upsert_preference to use single connection**

Replace the `upsert_preference` method in `src/mcp_awareness/postgres_store.py` (lines 290-325):

```python
    def upsert_preference(
        self, owner_id: str, key: str, scope: str, tags: list[str], data: dict[str, Any]
    ) -> Entry:
        """Upsert a preference by key + scope."""
        self._cleanup_expired()
        now = now_utc()
        with self._pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
            self._set_rls_context(cur, owner_id)
            cur.execute(
                _load_sql("select_preference_for_update"),
                (owner_id, EntryType.PREFERENCE.value, key, scope),
            )
            row = cur.fetchone()
            if row:
                e = self._row_to_entry(row)
                e.updated = now
                e.tags = tags
                e.data.update(data)
                cur.execute(
                    _load_sql("upsert_preference_update"),
                    (now, json.dumps(e.tags), json.dumps(e.data), e.id, owner_id),
                )
                return e
            entry = Entry(
                id=make_id(),
                type=EntryType.PREFERENCE,
                source=scope,
                tags=tags,
                created=now,
                expires=None,
                data=data,
            )
            self._insert_entry(cur, owner_id, entry)
        return entry
```

- [ ] **Step 5: Run tests to verify**

Run: `python -m pytest tests/test_store.py -k "upsert_preference" -v`
Expected: All PASS

- [ ] **Step 6: Commit**

```bash
git add src/mcp_awareness/postgres_store.py src/mcp_awareness/sql/select_preference_for_update.sql tests/test_store.py
git commit -m "fix: eliminate upsert_preference race condition with SELECT FOR UPDATE"
```

### Task 4: PR 1 changelog, full test run, and PR creation

**Files:**
- Modify: `CHANGELOG.md`

- [ ] **Step 1: Add changelog entries**

Under `[Unreleased]` in `CHANGELOG.md`, add under `### Fixed`:

```markdown
### Fixed
- `update_intention_state` now enforces `owner_id` in SQL WHERE clause, preventing cross-owner updates
- `upsert_alert` uses single-connection `SELECT FOR UPDATE` to prevent race conditions on concurrent upserts
- `upsert_preference` uses single-connection `SELECT FOR UPDATE` to prevent race conditions on concurrent upserts
```

- [ ] **Step 2: Run full test suite**

Run: `python -m pytest tests/ -v`
Expected: All tests PASS, no regressions

- [ ] **Step 3: Run linters**

Run: `ruff check src/ tests/ && ruff format --check src/ tests/ && mypy src/mcp_awareness/`
Expected: All clean

- [ ] **Step 4: Commit and push**

```bash
git add CHANGELOG.md
git commit -m "chore: add changelog entries for store safety fixes"
git push -u origin fix/store-safety
```

- [ ] **Step 5: Create PR**

```bash
gh pr create --title "Fix store safety: owner_id enforcement + upsert race conditions" --body "$(cat <<'EOF'
## Summary
- `update_intention_state` SQL now includes `AND owner_id = %s` — prevents cross-owner updates
- `upsert_alert` rewritten to use single connection with `SELECT FOR UPDATE` — eliminates read-modify-write race
- `upsert_preference` rewritten to use single connection with `SELECT FOR UPDATE` — eliminates read-modify-write race

Addresses 3 of 5 HIGH findings from the 2026-03-30 deep audit.

## QA

### Prerequisites
- `pip install -e ".[dev]"`
- Deploy to test instance on alternate port (`AWARENESS_PORT=8421`)

### Manual tests (via MCP tools)
1. - [ ] **Intention owner isolation**
   ```
   remember(description="owner-test intention", source="qa", tags=["qa-test"])
   ```
   Then attempt to update it from a different authenticated user — should fail silently (no state change).

2. - [ ] **Alert upsert idempotency**
   ```
   report_alert(source="qa-test", tags=["qa"], alert_id="race-1", level="warning", alert_type="threshold", message="test 1")
   report_alert(source="qa-test", tags=["qa"], alert_id="race-1", level="critical", alert_type="threshold", message="test 2")
   get_alerts(source="qa-test")
   ```
   Expected: Single alert with level=critical, message="test 2"

3. - [ ] **Preference upsert idempotency**
   ```
   set_preference(key="qa-theme", value="dark", scope="global", tags=["qa"])
   set_preference(key="qa-theme", value="light", scope="global", tags=["qa"])
   get_knowledge(entry_type="preference", tags=["qa"])
   ```
   Expected: Single preference with value="light"

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

---

## PR 2: Date Validation

Branch: `fix/date-validation`

### Task 5: Add _safe_ensure_dt helper and protect all date parsing

**Files:**
- Modify: `src/mcp_awareness/tools.py`
- Test: `tests/test_server.py`

- [ ] **Step 1: Write failing test — malformed date returns structured error**

Add to `tests/test_server.py`:

```python
class TestDateValidation:
    @pytest.mark.anyio
    async def test_get_alerts_malformed_since(self) -> None:
        result = await server_mod.get_alerts(since="not-a-date")
        parsed = json.loads(result)
        assert "error" in parsed or parsed.get("status") == "error"
        assert "date" in parsed.get("message", parsed.get("error", "")).lower()

    @pytest.mark.anyio
    async def test_get_knowledge_malformed_since(self) -> None:
        result = await server_mod.get_knowledge(since="not-a-date")
        parsed = json.loads(result)
        assert "error" in parsed or parsed.get("status") == "error"

    @pytest.mark.anyio
    async def test_get_knowledge_malformed_until(self) -> None:
        result = await server_mod.get_knowledge(until="2026-13-45")
        parsed = json.loads(result)
        assert "error" in parsed or parsed.get("status") == "error"

    @pytest.mark.anyio
    async def test_get_knowledge_malformed_created_after(self) -> None:
        result = await server_mod.get_knowledge(created_after="bad")
        parsed = json.loads(result)
        assert "error" in parsed or parsed.get("status") == "error"

    @pytest.mark.anyio
    async def test_remind_malformed_deliver_at(self) -> None:
        result = await server_mod.remind(
            goal="test", source="test", tags=["test"], deliver_at="not-a-date"
        )
        parsed = json.loads(result)
        assert "error" in parsed or parsed.get("status") == "error"

    @pytest.mark.anyio
    async def test_get_deleted_malformed_since(self) -> None:
        result = await server_mod.get_deleted(since="not-a-date")
        parsed = json.loads(result)
        assert "error" in parsed or parsed.get("status") == "error"

    @pytest.mark.anyio
    async def test_semantic_search_malformed_since(self) -> None:
        result = await server_mod.semantic_search(query="test", since="not-a-date")
        parsed = json.loads(result)
        assert "error" in parsed or parsed.get("status") == "error"

    @pytest.mark.anyio
    async def test_get_alerts_valid_date_still_works(self) -> None:
        result = await server_mod.get_alerts(since="2026-03-30T00:00:00Z")
        parsed = json.loads(result)
        assert isinstance(parsed, list)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_server.py::TestDateValidation -v`
Expected: FAIL — malformed dates raise unhandled ValueError

- [ ] **Step 3: Add _safe_ensure_dt helper**

In `src/mcp_awareness/tools.py`, add after the imports (after line 49):

```python
def _safe_ensure_dt(val: str | datetime) -> tuple[datetime | None, str | None]:
    """Wrap ensure_dt with error handling. Returns (datetime, None) or (None, error_message)."""
    try:
        return ensure_dt(val), None
    except (ValueError, TypeError) as exc:
        return None, f"Invalid date format: {exc}"
```

Also add the missing import at line 32:

```python
from datetime import datetime, timedelta
```

- [ ] **Step 4: Replace all unprotected ensure_dt/parse_iso calls**

Replace each bare `ensure_dt(x) if x else None` pattern with the safe version. Each call site follows this pattern:

```python
# Before:
since_dt = ensure_dt(since) if since else None

# After:
if since:
    since_dt, err = _safe_ensure_dt(since)
    if err:
        return json.dumps({"status": "error", "message": err})
else:
    since_dt = None
```

Apply to all 9 call sites in `tools.py`:
- Line 92: `get_alerts` — `since`
- Lines 169-172: `get_knowledge` — `since`, `until`, `created_after`, `created_before`
- Line 715: `get_deleted` — `since`
- Line 774: `get_reads` — `since`
- Line 796: `get_actions` — `since`
- Line 820: `get_unread` — `since`
- Line 839: `get_activity` — `since`
- Line 879: `remind` — `deliver_at`
- Lines 993-994: `semantic_search` — `since`, `until` (uses `parse_iso` — replace with `_safe_ensure_dt`)

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_server.py::TestDateValidation -v`
Expected: All PASS

- [ ] **Step 6: Run full test suite**

Run: `python -m pytest tests/ -v`
Expected: All tests PASS, no regressions

- [ ] **Step 7: Commit**

```bash
git add src/mcp_awareness/tools.py tests/test_server.py
git commit -m "fix: return structured errors for malformed date parameters"
```

### Task 6: PR 2 changelog, linters, and PR creation

**Files:**
- Modify: `CHANGELOG.md`

- [ ] **Step 1: Add changelog entry**

Under `[Unreleased]` in `CHANGELOG.md`, add under `### Fixed`:

```markdown
- All tools now return structured JSON errors for malformed date parameters instead of crashing with ValueError
```

- [ ] **Step 2: Run linters**

Run: `ruff check src/ tests/ && ruff format --check src/ tests/ && mypy src/mcp_awareness/`
Expected: All clean

- [ ] **Step 3: Commit and push**

```bash
git add CHANGELOG.md
git commit -m "chore: add changelog entry for date validation fix"
git push -u origin fix/date-validation
```

- [ ] **Step 4: Create PR**

```bash
gh pr create --title "Fix: structured errors for malformed date parameters" --body "$(cat <<'EOF'
## Summary
- Add `_safe_ensure_dt()` helper that wraps date parsing with try/except
- All 9 tool call sites now return `{"status": "error", "message": "Invalid date format: ..."}` instead of crashing
- Follows existing `_parse_entry_type()` pattern

Addresses 1 of 5 HIGH findings from the 2026-03-30 deep audit.

## QA

### Prerequisites
- `pip install -e ".[dev]"`
- Deploy to test instance on alternate port (`AWARENESS_PORT=8421`)

### Manual tests (via MCP tools)
1. - [ ] **Malformed since on get_alerts**
   ```
   get_alerts(since="not-a-date")
   ```
   Expected: `{"status": "error", "message": "Invalid date format: ..."}` (not a stack trace)

2. - [ ] **Malformed until on get_knowledge**
   ```
   get_knowledge(until="2026-13-45")
   ```
   Expected: `{"status": "error", "message": "Invalid date format: ..."}` (not a stack trace)

3. - [ ] **Malformed deliver_at on remind**
   ```
   remind(goal="test", source="test", tags=["test"], deliver_at="garbage")
   ```
   Expected: `{"status": "error", "message": "Invalid date format: ..."}` (not a stack trace)

4. - [ ] **Valid dates still work**
   ```
   get_alerts(since="2026-03-01T00:00:00Z")
   ```
   Expected: Normal JSON array response

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

---

## PR 3: Collator Batch Queries

Branch: `perf/collator-batch-queries`

### Task 7: Add batch query methods to Store protocol

**Files:**
- Modify: `src/mcp_awareness/store.py`

- [ ] **Step 1: Add 4 batch method signatures to Store protocol**

Add after the `get_patterns` method (~line 98) in `src/mcp_awareness/store.py`:

```python
    def get_all_statuses(self, owner_id: str) -> dict[str, Entry]:
        """Get latest status for every source. Returns {source: Entry}."""
        ...

    def get_all_active_alerts(self, owner_id: str) -> dict[str, list[Entry]]:
        """Get all non-resolved alerts grouped by source. Returns {source: [Entry]}."""
        ...

    def get_all_active_suppressions(self, owner_id: str) -> dict[str, list[Entry]]:
        """Get all active suppressions grouped by source. Includes global (empty source) under key ''."""
        ...

    def get_all_patterns(self, owner_id: str) -> dict[str, list[Entry]]:
        """Get all patterns grouped by source. Includes global (empty source) under key ''."""
        ...
```

- [ ] **Step 2: Commit**

```bash
git add src/mcp_awareness/store.py
git commit -m "feat: add batch query method signatures to Store protocol"
```

### Task 8: Implement batch queries in PostgresStore

**Files:**
- Modify: `src/mcp_awareness/postgres_store.py`
- Create: `src/mcp_awareness/sql/get_all_statuses.sql`
- Create: `src/mcp_awareness/sql/get_all_active_alerts.sql`
- Create: `src/mcp_awareness/sql/get_all_active_suppressions.sql`
- Create: `src/mcp_awareness/sql/get_all_patterns.sql`
- Test: `tests/test_store.py`

- [ ] **Step 1: Write failing tests for batch methods**

Add to `tests/test_store.py`:

```python
def test_get_all_statuses(store):
    """get_all_statuses returns {source: Entry} for all sources."""
    store.upsert_status(TEST_OWNER, "nas", ["infra"], {"metrics": {"cpu": 50}, "ttl_sec": 3600})
    store.upsert_status(TEST_OWNER, "ci", ["cicd"], {"metrics": {}, "ttl_sec": 3600})
    result = store.get_all_statuses(TEST_OWNER)
    assert set(result.keys()) == {"nas", "ci"}
    assert result["nas"].source == "nas"
    assert result["ci"].source == "ci"


def test_get_all_statuses_empty(store):
    """get_all_statuses returns empty dict when no statuses exist."""
    assert store.get_all_statuses(TEST_OWNER) == {}


def test_get_all_active_alerts(store):
    """get_all_active_alerts returns {source: [Entry]} for all sources."""
    store.upsert_alert(
        TEST_OWNER, "nas", ["infra"], "a1",
        {"alert_id": "a1", "level": "warning", "message": "NAS issue", "resolved": False},
    )
    store.upsert_alert(
        TEST_OWNER, "ci", ["cicd"], "a2",
        {"alert_id": "a2", "level": "warning", "message": "CI issue", "resolved": False},
    )
    store.upsert_alert(
        TEST_OWNER, "nas", ["infra"], "a3",
        {"alert_id": "a3", "level": "critical", "message": "NAS critical", "resolved": False},
    )
    result = store.get_all_active_alerts(TEST_OWNER)
    assert set(result.keys()) == {"nas", "ci"}
    assert len(result["nas"]) == 2
    assert len(result["ci"]) == 1


def test_get_all_active_alerts_excludes_resolved(store):
    """get_all_active_alerts excludes resolved alerts."""
    store.upsert_alert(
        TEST_OWNER, "nas", ["infra"], "a1",
        {"alert_id": "a1", "level": "warning", "message": "resolved", "resolved": True},
    )
    result = store.get_all_active_alerts(TEST_OWNER)
    assert result == {}


def test_get_all_active_suppressions(store):
    """get_all_active_suppressions groups by source, includes global."""
    store.add(TEST_OWNER, Entry(
        id=make_id(), type=EntryType.SUPPRESSION, source="nas", tags=["infra"],
        created=now_utc(), expires=None, data={"metric": "cpu", "reason": "maintenance"},
    ))
    store.add(TEST_OWNER, Entry(
        id=make_id(), type=EntryType.SUPPRESSION, source="", tags=[],
        created=now_utc(), expires=None, data={"metric": "all", "reason": "global"},
    ))
    result = store.get_all_active_suppressions(TEST_OWNER)
    assert "nas" in result
    assert "" in result
    assert len(result["nas"]) == 1
    assert len(result[""]) == 1


def test_get_all_patterns(store):
    """get_all_patterns groups by source, includes global."""
    store.add(TEST_OWNER, Entry(
        id=make_id(), type=EntryType.PATTERN, source="nas", tags=["infra"],
        created=now_utc(), expires=None,
        data={"effect": "CPU spike during backup", "conditions": {"hour_range": "02:00-04:00"}},
    ))
    store.add(TEST_OWNER, Entry(
        id=make_id(), type=EntryType.PATTERN, source="", tags=[],
        created=now_utc(), expires=None,
        data={"effect": "Global pattern", "conditions": {}},
    ))
    result = store.get_all_patterns(TEST_OWNER)
    assert "nas" in result
    assert "" in result
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_store.py -k "get_all_" -v`
Expected: FAIL — methods don't exist yet

- [ ] **Step 3: Create SQL files**

Create `src/mcp_awareness/sql/get_all_statuses.sql`:

```sql
/* name: get_all_statuses */
/* mode: literal */
/* Get the latest status entry per source using DISTINCT ON.
   Params: owner_id, type
*/
SELECT DISTINCT ON (source) * FROM entries
WHERE owner_id = %s AND type = %s AND deleted IS NULL
ORDER BY source, created DESC
```

Create `src/mcp_awareness/sql/get_all_active_alerts.sql`:

```sql
/* name: get_all_active_alerts */
/* mode: literal */
/* Get all non-resolved, non-deleted alert entries.
   Params: owner_id, type
*/
SELECT * FROM entries
WHERE owner_id = %s AND type = %s AND deleted IS NULL
  AND NOT (data @> '{"resolved": true}'::jsonb)
ORDER BY COALESCE(updated, created) DESC
```

Create `src/mcp_awareness/sql/get_all_active_suppressions.sql`:

```sql
/* name: get_all_active_suppressions */
/* mode: literal */
/* Get all non-expired suppression entries.
   Params: owner_id, type
*/
SELECT * FROM entries
WHERE owner_id = %s AND type = %s AND deleted IS NULL
  AND (expires IS NULL OR expires > NOW())
ORDER BY COALESCE(updated, created) DESC
```

Create `src/mcp_awareness/sql/get_all_patterns.sql`:

```sql
/* name: get_all_patterns */
/* mode: literal */
/* Get all pattern entries.
   Params: owner_id, type
*/
SELECT * FROM entries
WHERE owner_id = %s AND type = %s AND deleted IS NULL
ORDER BY COALESCE(updated, created) DESC
```

- [ ] **Step 4: Implement batch methods in PostgresStore**

Add to `src/mcp_awareness/postgres_store.py` after the `get_patterns` method (~line 414). Add `from collections import defaultdict` to imports at the top of the file.

```python
    def get_all_statuses(self, owner_id: str) -> dict[str, Entry]:
        with self._pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
            self._set_rls_context(cur, owner_id)
            cur.execute(
                _load_sql("get_all_statuses"),
                (owner_id, EntryType.STATUS.value),
            )
            return {row["source"]: self._row_to_entry(row) for row in cur.fetchall()}

    def get_all_active_alerts(self, owner_id: str) -> dict[str, list[Entry]]:
        with self._pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
            self._set_rls_context(cur, owner_id)
            cur.execute(
                _load_sql("get_all_active_alerts"),
                (owner_id, EntryType.ALERT.value),
            )
            result: dict[str, list[Entry]] = defaultdict(list)
            for row in cur.fetchall():
                result[row["source"]].append(self._row_to_entry(row))
            return dict(result)

    def get_all_active_suppressions(self, owner_id: str) -> dict[str, list[Entry]]:
        with self._pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
            self._set_rls_context(cur, owner_id)
            cur.execute(
                _load_sql("get_all_active_suppressions"),
                (owner_id, EntryType.SUPPRESSION.value),
            )
            result: dict[str, list[Entry]] = defaultdict(list)
            for row in cur.fetchall():
                result[row["source"]].append(self._row_to_entry(row))
            return dict(result)

    def get_all_patterns(self, owner_id: str) -> dict[str, list[Entry]]:
        with self._pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
            self._set_rls_context(cur, owner_id)
            cur.execute(
                _load_sql("get_all_patterns"),
                (owner_id, EntryType.PATTERN.value),
            )
            result: dict[str, list[Entry]] = defaultdict(list)
            for row in cur.fetchall():
                result[row["source"]].append(self._row_to_entry(row))
            return dict(result)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_store.py -k "get_all_" -v`
Expected: All PASS

- [ ] **Step 6: Commit**

```bash
git add src/mcp_awareness/postgres_store.py src/mcp_awareness/sql/get_all_*.sql tests/test_store.py
git commit -m "feat: implement batch query methods in PostgresStore"
```

### Task 9: Refactor generate_briefing to use batch queries

**Files:**
- Modify: `src/mcp_awareness/collator.py:283-339`
- Test: `tests/test_collator.py`

- [ ] **Step 1: Write regression test to capture current briefing output**

Add to `tests/test_collator.py`:

```python
    def test_batch_queries_match_per_source(self, store):
        """Batch-query briefing produces identical output to per-source queries."""
        store.upsert_status(TEST_OWNER, "nas", ["infra"], {"metrics": {"cpu": 50}, "ttl_sec": 3600})
        store.upsert_status(TEST_OWNER, "ci", ["cicd"], {"metrics": {}, "ttl_sec": 3600})
        store.upsert_alert(
            TEST_OWNER, "nas", ["infra"], "cpu-1",
            {"alert_id": "cpu-1", "level": "warning", "alert_type": "threshold",
             "message": "CPU at 96%", "resolved": False},
        )
        store.add(TEST_OWNER, Entry(
            id=make_id(), type=EntryType.SUPPRESSION, source="ci", tags=["cicd"],
            created=now_utc(), expires=None, data={"metric": "build", "reason": "expected"},
        ))
        store.add(TEST_OWNER, Entry(
            id=make_id(), type=EntryType.PATTERN, source="nas", tags=["infra"],
            created=now_utc(), expires=None,
            data={"effect": "disk spike during backup", "conditions": {"hour_range": "02:00-04:00"}},
        ))
        briefing = generate_briefing(store, TEST_OWNER)
        assert briefing["attention_needed"] is True
        assert briefing["active_alerts"] == 1
        assert set(briefing["sources"].keys()) == {"nas", "ci"}
        assert briefing["sources"]["nas"]["status"] == "warning"
        assert briefing["sources"]["ci"]["status"] == "ok"
```

- [ ] **Step 2: Run test to verify it passes with current code**

Run: `python -m pytest tests/test_collator.py::TestGenerateBriefing::test_batch_queries_match_per_source -v`
Expected: PASS (baseline)

- [ ] **Step 3: Refactor generate_briefing to use batch queries**

Replace the per-source loop in `src/mcp_awareness/collator.py` (lines 283-339) with:

```python
    # Batch-fetch all data in 4 queries (fixed cost regardless of source count)
    all_statuses = store.get_all_statuses(owner_id)
    all_alerts = store.get_all_active_alerts(owner_id)
    all_suppressions = store.get_all_active_suppressions(owner_id)
    all_patterns = store.get_all_patterns(owner_id)
    global_suppressions = all_suppressions.get("", [])
    global_patterns = all_patterns.get("", [])

    for source in store.get_sources(owner_id):
        status = all_statuses.get(source)
        alerts = all_alerts.get(source, [])
        suppressions = all_suppressions.get(source, []) + global_suppressions
        patterns = all_patterns.get(source, []) + global_patterns

        # Check for stale sources (TTL expired) — alerts from stale sources
        # are not evaluated (suppression/pattern filtering is skipped)
        if status and status.is_stale():
            eval_stale_sources += 1
            age = int(status.age_sec)
            briefing["sources"][source] = {
                "status": "stale",
                "last_report": to_iso(status.updated or status.created),
                "headline": f"{source} has not reported in {age}s",
                "drill_down": f"awareness://status/{source}",
            }
            briefing["attention_needed"] = True
            continue

        # Count alerts only for non-stale sources (we actually evaluate these)
        eval_alerts_checked += len(alerts)

        # Apply suppressions — filter out suppressed alerts
        pre_suppression = len(alerts)
        active_alerts = [a for a in alerts if not is_suppressed(a, suppressions)]
        eval_suppressed += pre_suppression - len(active_alerts)

        # Apply learned patterns — filter out expected anomalies
        pre_pattern = len(active_alerts)
        active_alerts = [a for a in active_alerts if not matches_pattern(a, patterns)]
        eval_pattern_matched += pre_pattern - len(active_alerts)

        # Determine source status
        if any(a.data.get("level") == "critical" for a in active_alerts):
            source_status = "critical"
        elif active_alerts:
            source_status = "warning"
        else:
            source_status = "ok"

        source_entry: dict[str, Any] = {
            "status": source_status,
            "last_report": to_iso(status.updated or status.created) if status else None,
        }

        if active_alerts:
            top_alert = max(
                active_alerts,
                key=lambda a: severity_rank(a.data.get("level", "warning")),
            )
            source_entry["headline"] = top_alert.data.get("message", "")
            source_entry["drill_down"] = f"awareness://alerts/{source}"
            briefing["active_alerts"] += len(active_alerts)
            briefing["attention_needed"] = True

        briefing["sources"][source] = source_entry
```

- [ ] **Step 4: Run regression test**

Run: `python -m pytest tests/test_collator.py::TestGenerateBriefing -v`
Expected: All PASS — identical behavior

- [ ] **Step 5: Run full test suite**

Run: `python -m pytest tests/ -v`
Expected: All tests PASS

- [ ] **Step 6: Commit**

```bash
git add src/mcp_awareness/collator.py
git commit -m "perf: replace N+1 queries with batch fetch in generate_briefing"
```

### Task 10: PR 3 changelog, linters, and PR creation

**Files:**
- Modify: `CHANGELOG.md`

- [ ] **Step 1: Add changelog entry**

Under `[Unreleased]` in `CHANGELOG.md`, add under `### Changed`:

```markdown
### Changed
- `generate_briefing` now uses 5 fixed queries instead of 3-4 per source (N+1 → batch)
```

- [ ] **Step 2: Run linters**

Run: `ruff check src/ tests/ && ruff format --check src/ tests/ && mypy src/mcp_awareness/`
Expected: All clean

- [ ] **Step 3: Commit and push**

```bash
git add CHANGELOG.md
git commit -m "chore: add changelog entry for collator batch queries"
git push -u origin perf/collator-batch-queries
```

- [ ] **Step 4: Create PR**

```bash
gh pr create --title "Perf: batch queries in generate_briefing (N+1 elimination)" --body "$(cat <<'EOF'
## Summary
- Add 4 batch query methods to Store protocol: `get_all_statuses`, `get_all_active_alerts`, `get_all_active_suppressions`, `get_all_patterns`
- Refactor `generate_briefing()` to call them once upfront instead of 3-4 queries per source
- 20 sources: 80+ queries → 5 queries (fixed cost)
- No behavior change — output is identical

Addresses 1 of 5 HIGH findings from the 2026-03-30 deep audit.

## QA

### Prerequisites
- `pip install -e ".[dev]"`
- Deploy to test instance on alternate port (`AWARENESS_PORT=8421`)

### Manual tests (via MCP tools)
1. - [ ] **Briefing with multiple sources**
   ```
   report_status(source="qa-test-1", tags=["qa"], metrics={"cpu": 50}, ttl_sec=3600)
   report_status(source="qa-test-2", tags=["qa"], metrics={"mem": 70}, ttl_sec=3600)
   report_alert(source="qa-test-1", tags=["qa"], alert_id="a1", level="warning", alert_type="threshold", message="CPU high")
   get_briefing()
   ```
   Expected: Briefing shows both sources, qa-test-1 with warning status, qa-test-2 with ok status

2. - [ ] **Briefing with suppressions**
   ```
   suppress_alert(source="qa-test-1", tags=["qa"], reason="maintenance", duration_hours=1)
   get_briefing()
   ```
   Expected: qa-test-1 alert suppressed, attention_needed reflects remaining alerts only

3. - [ ] **Empty briefing**
   ```
   get_briefing()
   ```
   Expected: `attention_needed: false`, empty sources (after cleanup)

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```
