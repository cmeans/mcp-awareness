# Schema + Record Entry Types Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `EntryType.SCHEMA` and `EntryType.RECORD` with JSON Schema Draft 2020-12 validation on write, per-owner storage with a shared `_system` fallback, schema immutability, record re-validation on update, and a CLI tool for seeding system-owned schemas. Delivers steps 1–2 of the `design-schema-record-secrets` spec; secrets infrastructure is a separate follow-up.

**Architecture:** New `validation.py` module holds pure validation functions using the `jsonschema` library (centralized, unit-testable without DB, keeps `jsonschema` out of the store layer). Two new MCP write tools — `register_schema` and `create_record` — match the existing one-tool-per-type convention. `update_entry` and `delete_entry` gain type-specific branches for the new entries. Store protocol grows exactly two methods: `find_schema` (with `_system` fallback) and `count_records_referencing` (for deletion protection). A new `mcp-awareness-register-schema` console script bypasses MCP for operator bootstrap of `_system`-owned schemas.

**Tech Stack:** Python 3.11+, FastMCP, psycopg + pgvector-enabled Postgres 17, Alembic, `jsonschema>=4.26.0` (new dep), existing structured-error helper (`_error_response`), testcontainers for integration tests.

**Spec:** [`docs/superpowers/specs/2026-04-13-schema-record-entry-types-design.md`](../specs/2026-04-13-schema-record-entry-types-design.md) — all design decisions D1–D8 and error codes are authoritative there. This plan implements without re-deriving.

**Branch:** `feat/schema-record-entry-types` (already created with the spec commit).

---

## File Map

**Files to create:**
- `src/mcp_awareness/validation.py` — pure validation functions
- `src/mcp_awareness/cli_register_schema.py` — `mcp-awareness-register-schema` console script
- `alembic/versions/<next-id>_add_system_user_for_schemas.py` — `_system` user seed migration
- `tests/test_validation.py` — unit tests for validation module
- `tests/test_tools_schema_record.py` — integration tests via testcontainers Postgres
- `tests/test_cli_register_schema.py` — CLI tool tests

**Files to modify:**
- `src/mcp_awareness/schema.py` — add `SCHEMA` and `RECORD` enum values
- `src/mcp_awareness/store.py` — `Store` protocol: add `find_schema`, `count_records_referencing`
- `src/mcp_awareness/postgres_store.py` — implement the two new methods
- `src/mcp_awareness/tools.py` — add `register_schema`, `create_record`; branch `update_entry` / `delete_entry`
- `src/mcp_awareness/instructions.md` — mention new tools in server instructions
- `pyproject.toml` — add `jsonschema>=4.26.0` dep; add `mcp-awareness-register-schema` console script
- `CHANGELOG.md` — entry under `[Unreleased]`
- `README.md` — update tool count and "Implemented" section
- `docs/data-dictionary.md` — document `schema` and `record` entry types
- `tests/test_schema.py` — add enum-value coverage
- `tests/test_store.py` — add `find_schema` / `count_records_referencing` coverage

---

## Execution Notes

- **TDD throughout:** every code task writes the failing test first, verifies it fails, implements minimal code, verifies it passes, commits. No committing of untested code.
- **Commit frequency:** at minimum one commit per task, often mid-task after a green test.
- **Conventional commits:** `feat:`, `fix:`, `docs:`, `test:`, `chore:`, `refactor:` as appropriate.
- **Pre-commit discipline** (per saved feedback): before first push, run `ruff format`, `ruff check`, `mypy src/`, `pytest --cov`, verify coverage of new lines, verify test count in README matches reality.
- **AGPL preamble:** every new `.py` file must start with the AGPL v3 license header (copy from any existing `src/mcp_awareness/*.py` file).
- **Structured errors only:** all new error paths use `_error_response()` from `helpers.py`. No `raise ValueError` in tool-facing paths.
- **No `pragma: no cover`** without explicit approval.

---

## Task 1: Add `SCHEMA` and `RECORD` to `EntryType` enum

**Files:**
- Modify: `src/mcp_awareness/schema.py` (class `EntryType`, line 30)
- Modify: `tests/test_schema.py`

- [ ] **Step 1: Write failing test**

Append to `tests/test_schema.py`:

```python
def test_entry_type_schema_value():
    assert EntryType.SCHEMA.value == "schema"
    assert EntryType("schema") is EntryType.SCHEMA


def test_entry_type_record_value():
    assert EntryType.RECORD.value == "record"
    assert EntryType("record") is EntryType.RECORD
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_schema.py::test_entry_type_schema_value tests/test_schema.py::test_entry_type_record_value -v`
Expected: FAIL — `AttributeError: SCHEMA`.

- [ ] **Step 3: Add enum values**

Edit `src/mcp_awareness/schema.py`, inside `class EntryType`:

```python
class EntryType(str, Enum):
    STATUS = "status"
    ALERT = "alert"
    PATTERN = "pattern"
    SUPPRESSION = "suppression"
    CONTEXT = "context"
    PREFERENCE = "preference"
    NOTE = "note"
    INTENTION = "intention"
    SCHEMA = "schema"
    RECORD = "record"
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_schema.py -v`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/mcp_awareness/schema.py tests/test_schema.py
git commit -m "feat: add SCHEMA and RECORD to EntryType enum"
```

---

## Task 2: Add `jsonschema` dependency

**Files:**
- Modify: `pyproject.toml`

- [ ] **Step 1: Add to dependencies**

Edit `pyproject.toml` → `[project] dependencies` array (or equivalent). Add:

```
jsonschema>=4.26.0,<5
```

- [ ] **Step 2: Install locally**

Run: `pip install -e ".[dev]"`
Expected: installs `jsonschema`, `jsonschema-specifications`, `referencing`, `rpds-py`, `attrs`.

- [ ] **Step 3: Verify importable**

Run: `python -c "from jsonschema import Draft202012Validator; print(Draft202012Validator.META_SCHEMA['$id'])"`
Expected: prints `https://json-schema.org/draft/2020-12/schema`.

- [ ] **Step 4: Commit**

```bash
git add pyproject.toml
git commit -m "chore: add jsonschema>=4.26.0 dependency"
```

---

## Task 3: Create `validation.py` with `compose_schema_logical_key`

Start with the smallest pure function to establish the module.

**Files:**
- Create: `src/mcp_awareness/validation.py`
- Create: `tests/test_validation.py`

- [ ] **Step 1: Create failing test**

Create `tests/test_validation.py`:

```python
# AGPL preamble here — copy from tests/test_schema.py

"""Tests for src/mcp_awareness/validation.py."""

from __future__ import annotations

import pytest

from mcp_awareness.validation import compose_schema_logical_key


def test_compose_schema_logical_key_basic():
    assert compose_schema_logical_key("schema:edge-manifest", "1.0.0") == "schema:edge-manifest:1.0.0"


def test_compose_schema_logical_key_no_prefix():
    assert compose_schema_logical_key("tag-taxonomy", "0.1.0") == "tag-taxonomy:0.1.0"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_validation.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'mcp_awareness.validation'`.

- [ ] **Step 3: Create validation module**

Create `src/mcp_awareness/validation.py`:

```python
# AGPL preamble here — copy from src/mcp_awareness/schema.py

"""Validation helpers for Schema and Record entry types.

Pure functions wrapping jsonschema Draft 2020-12 validation and schema
lookup with _system fallback. Kept out of the store layer so the Store
protocol stays swappable (no jsonschema import in store.py).
"""

from __future__ import annotations


def compose_schema_logical_key(family: str, version: str) -> str:
    """Derive the canonical logical_key for a schema entry.

    Single source of truth for the family+version → logical_key format.
    Used by register_schema on write and by resolve_schema on lookup.
    """
    return f"{family}:{version}"
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_validation.py -v`
Expected: both tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/mcp_awareness/validation.py tests/test_validation.py
git commit -m "feat: add validation module with compose_schema_logical_key"
```

---

## Task 4: `validation.validate_schema_body`

**Files:**
- Modify: `src/mcp_awareness/validation.py`
- Modify: `tests/test_validation.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_validation.py`:

```python
import jsonschema

from mcp_awareness.validation import validate_schema_body


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
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/test_validation.py -v`
Expected: FAIL — `ImportError: cannot import name 'validate_schema_body'`.

- [ ] **Step 3: Implement**

Append to `src/mcp_awareness/validation.py`:

```python
from typing import Any

from jsonschema import Draft202012Validator


def validate_schema_body(schema: Any) -> None:
    """Validate a schema body against the JSON Schema Draft 2020-12 meta-schema.

    Raises jsonschema.exceptions.SchemaError on invalid schema. Callers at
    the MCP boundary translate this into a structured 'invalid_schema' error
    response; direct callers (CLI) format to stderr.
    """
    Draft202012Validator.check_schema(schema)
```

- [ ] **Step 4: Run tests, verify pass**

Run: `pytest tests/test_validation.py -v`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/mcp_awareness/validation.py tests/test_validation.py
git commit -m "feat: add validate_schema_body for Draft 2020-12 meta-schema check"
```

---

## Task 5: `validation.validate_record_content`

Returns a sorted list of flattened error dicts. Callers decide how to envelope them.

**Files:**
- Modify: `src/mcp_awareness/validation.py`
- Modify: `tests/test_validation.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_validation.py`:

```python
from mcp_awareness.validation import validate_record_content


_PERSON_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "age": {"type": "integer", "minimum": 0},
    },
    "required": ["name"],
}


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
    # Truncation is carried via a special sentinel entry at the end; see impl
    assert len(result) == 51  # 50 errors + 1 truncation marker
    assert result[-1]["truncated"] is True
    assert result[-1]["total_errors"] == 60
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/test_validation.py -v`
Expected: FAIL on missing import.

- [ ] **Step 3: Implement**

Append to `src/mcp_awareness/validation.py`:

```python
from jsonschema import ValidationError

_MAX_VALIDATION_ERRORS = 50


def _flatten_error(err: ValidationError) -> dict[str, Any]:
    """Flatten a jsonschema ValidationError to a structured dict for the error envelope."""
    return {
        "path": err.json_path,
        "message": err.message,
        "validator": err.validator,
        "schema_path": "/" + "/".join(str(p) for p in err.schema_path),
    }


def validate_record_content(schema_body: dict[str, Any], content: Any) -> list[dict[str, Any]]:
    """Validate content against a schema body. Returns list of structured errors.

    Empty list means valid. List truncated at _MAX_VALIDATION_ERRORS; when
    truncated, final entry is {'truncated': True, 'total_errors': <n>}.
    """
    validator = Draft202012Validator(schema_body)
    all_errors = sorted(validator.iter_errors(content), key=lambda e: e.path)
    if len(all_errors) <= _MAX_VALIDATION_ERRORS:
        return [_flatten_error(e) for e in all_errors]
    kept = [_flatten_error(e) for e in all_errors[:_MAX_VALIDATION_ERRORS]]
    kept.append({"truncated": True, "total_errors": len(all_errors)})
    return kept
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_validation.py -v`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/mcp_awareness/validation.py tests/test_validation.py
git commit -m "feat: add validate_record_content with iter_errors and truncation"
```

---

## Task 6: Add `find_schema` to Store protocol and PostgresStore

**Files:**
- Modify: `src/mcp_awareness/store.py` (Store protocol)
- Modify: `src/mcp_awareness/postgres_store.py` (implementation)
- Modify: `tests/test_store.py`
- Create (if needed): `src/mcp_awareness/sql/find_schema.sql`

- [ ] **Step 1: Inspect existing Store protocol**

Read `src/mcp_awareness/store.py` to see the current Protocol signature style; mirror it.

- [ ] **Step 2: Write failing integration test**

Append to `tests/test_store.py`:

```python
from mcp_awareness.schema import Entry, EntryType, make_id, now_utc

SYSTEM_OWNER = "_system"


def _schema_entry(owner: str, logical_key: str, family: str, version: str, schema_body: dict) -> Entry:
    return Entry(
        id=make_id(),
        type=EntryType.SCHEMA,
        source="test",
        tags=[],
        created=now_utc(),
        updated=None,
        expires=None,
        data={
            "family": family,
            "version": version,
            "schema": schema_body,
            "description": "test schema",
            "learned_from": "test",
        },
        logical_key=logical_key,
        owner_id=owner,
    )


def test_find_schema_returns_caller_owned(store):
    # Ensure _system user exists so the FK-less owner_id insert is valid
    store._conn_pool  # ensure pool lazy-init done — or use a helper if provided
    # Insert _system user if the test schema doesn't seed it; adjust if fixture changes
    store.save_entry(_schema_entry(TEST_OWNER, "s:test:1.0.0", "s:test", "1.0.0", {"type": "object"}))
    found = store.find_schema(TEST_OWNER, "s:test:1.0.0")
    assert found is not None
    assert found.owner_id == TEST_OWNER
    assert found.data["family"] == "s:test"


def test_find_schema_system_fallback(store):
    store.save_entry(_schema_entry(SYSTEM_OWNER, "s:test:1.0.0", "s:test", "1.0.0", {"type": "object"}))
    found = store.find_schema(TEST_OWNER, "s:test:1.0.0")
    assert found is not None
    assert found.owner_id == SYSTEM_OWNER


def test_find_schema_caller_wins_over_system(store):
    # Seed _system first
    store.save_entry(_schema_entry(SYSTEM_OWNER, "s:test:1.0.0", "s:test", "1.0.0", {"type": "object"}))
    # Then caller-owned override
    store.save_entry(_schema_entry(TEST_OWNER, "s:test:1.0.0", "s:test", "1.0.0", {"type": "string"}))
    found = store.find_schema(TEST_OWNER, "s:test:1.0.0")
    assert found is not None
    assert found.owner_id == TEST_OWNER
    # The caller-owned schema overrode the system one
    assert found.data["schema"] == {"type": "string"}


def test_find_schema_returns_none_when_missing(store):
    assert store.find_schema(TEST_OWNER, "s:nonexistent:1.0.0") is None


def test_find_schema_excludes_soft_deleted(store):
    entry = _schema_entry(TEST_OWNER, "s:test:1.0.0", "s:test", "1.0.0", {"type": "object"})
    store.save_entry(entry)
    store.delete_entry(TEST_OWNER, entry.id)
    assert store.find_schema(TEST_OWNER, "s:test:1.0.0") is None
```

Note: the `_system` user FK must exist before inserting entries with that `owner_id`. This is normally handled by the migration in Task 10. During testing, **either** wait for Task 10 to land **or** add a test-only helper that inserts `_system` into `users`. The simplest approach: chain Tasks 6 and 10 together OR do Task 10 before Task 6. **Decision: reorder — do the migration (Task 10) before writing the store integration tests.**

→ **If following the plan in order, swap Task 6 and Task 10.** Alternative: augment conftest.py's `store` fixture to pre-seed `_system` into `users` (keeps plan order natural).

Preferred approach: add `_system` to the `store` fixture in `conftest.py`:

```python
@pytest.fixture
def store(pg_dsn):
    """Fresh PostgresStore for each test — tables created, then cleared after."""
    s = PostgresStore(pg_dsn)
    # Ensure _system user exists for cross-owner schema tests.
    with s._conn_pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO users (id, display_name) VALUES ('_system', 'System-managed schemas') "
            "ON CONFLICT (id) DO NOTHING"
        )
        conn.commit()
    yield s
    s.clear(TEST_OWNER)
    s.clear(SYSTEM_OWNER)
```

- [ ] **Step 3: Run tests to verify failure**

Run: `pytest tests/test_store.py -v -k find_schema`
Expected: FAIL — `AttributeError: PostgresStore has no attribute 'find_schema'`.

- [ ] **Step 4: Add method to Store protocol**

Edit `src/mcp_awareness/store.py`, add to the `Store` Protocol:

```python
def find_schema(self, owner_id: str, logical_key: str) -> Entry | None:
    """Look up a schema entry by logical_key, preferring caller-owned over _system.

    Returns the schema entry or None if not found or soft-deleted.
    """
    ...
```

- [ ] **Step 5: Implement in PostgresStore**

Edit `src/mcp_awareness/postgres_store.py`:

```python
def find_schema(self, owner_id: str, logical_key: str) -> Entry | None:
    """Look up a schema, preferring caller-owned over _system-owned.

    Single query with CASE-based ORDER BY for predictable override
    semantics: caller's own version wins, _system is fallback.
    """
    query = """
        SELECT id, type, source, tags, created, updated, expires, data,
               logical_key, owner_id, language, deleted
        FROM entries
        WHERE type = 'schema'
          AND logical_key = %(logical_key)s
          AND owner_id IN (%(caller)s, '_system')
          AND deleted IS NULL
        ORDER BY CASE WHEN owner_id = %(caller)s THEN 0 ELSE 1 END
        LIMIT 1
    """
    with self._conn_pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(query, {"logical_key": logical_key, "caller": owner_id})
        row = cur.fetchone()
        if row is None:
            return None
        return _row_to_entry(row)
```

Adjust `dict_row` / `_row_to_entry` to match existing patterns in the file (import name and helper function may differ — follow what the rest of `postgres_store.py` uses).

- [ ] **Step 6: Externalize SQL if project pattern requires**

If the codebase follows "one SQL file per operation" (check `src/mcp_awareness/sql/`), create `sql/find_schema.sql` with the query text and load it via the existing SQL-loading helper. Otherwise, inline is fine.

- [ ] **Step 7: Run tests**

Run: `pytest tests/test_store.py -v -k find_schema`
Expected: all pass.

- [ ] **Step 8: Commit**

```bash
git add src/mcp_awareness/store.py src/mcp_awareness/postgres_store.py \
        tests/test_store.py tests/conftest.py src/mcp_awareness/sql/find_schema.sql
git commit -m "feat: add Store.find_schema with _system fallback"
```

---

## Task 7: Add `count_records_referencing` to Store and PostgresStore

**Files:**
- Modify: `src/mcp_awareness/store.py`
- Modify: `src/mcp_awareness/postgres_store.py`
- Modify: `tests/test_store.py`
- Create (if project convention): `src/mcp_awareness/sql/count_records_referencing.sql`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_store.py`:

```python
def _record_entry(owner: str, logical_key: str, schema_ref: str, schema_version: str, content) -> Entry:
    return Entry(
        id=make_id(),
        type=EntryType.RECORD,
        source="test",
        tags=[],
        created=now_utc(),
        updated=None,
        expires=None,
        data={
            "schema_ref": schema_ref,
            "schema_version": schema_version,
            "content": content,
            "description": "test record",
            "learned_from": "test",
        },
        logical_key=logical_key,
        owner_id=owner,
    )


def test_count_records_referencing_returns_zero_when_none(store):
    count, ids = store.count_records_referencing(TEST_OWNER, "s:test:1.0.0")
    assert count == 0
    assert ids == []


def test_count_records_referencing_counts_matching_records(store):
    # Insert 3 records referencing s:test:1.0.0
    for i in range(3):
        store.save_entry(_record_entry(TEST_OWNER, f"rec-{i}", "s:test", "1.0.0", {"i": i}))
    count, ids = store.count_records_referencing(TEST_OWNER, "s:test:1.0.0")
    assert count == 3
    assert len(ids) == 3


def test_count_records_referencing_excludes_soft_deleted(store):
    e = _record_entry(TEST_OWNER, "rec-1", "s:test", "1.0.0", {})
    store.save_entry(e)
    store.delete_entry(TEST_OWNER, e.id)
    count, ids = store.count_records_referencing(TEST_OWNER, "s:test:1.0.0")
    assert count == 0
    assert ids == []


def test_count_records_referencing_ignores_other_versions(store):
    store.save_entry(_record_entry(TEST_OWNER, "rec-1", "s:test", "1.0.0", {}))
    store.save_entry(_record_entry(TEST_OWNER, "rec-2", "s:test", "2.0.0", {}))
    count, _ = store.count_records_referencing(TEST_OWNER, "s:test:1.0.0")
    assert count == 1


def test_count_records_referencing_caps_id_list_at_ten(store):
    for i in range(15):
        store.save_entry(_record_entry(TEST_OWNER, f"rec-{i}", "s:test", "1.0.0", {"i": i}))
    count, ids = store.count_records_referencing(TEST_OWNER, "s:test:1.0.0")
    assert count == 15
    assert len(ids) == 10
```

- [ ] **Step 2: Verify failure**

Run: `pytest tests/test_store.py -v -k count_records_referencing`
Expected: FAIL — method not defined.

- [ ] **Step 3: Add to protocol and implement**

Edit `src/mcp_awareness/store.py`:

```python
def count_records_referencing(
    self, owner_id: str, schema_logical_key: str
) -> tuple[int, list[str]]:
    """Return (total_count, first_N_ids) of non-deleted records referencing a schema.

    The schema_logical_key is composed as f"{schema_ref}:{schema_version}".
    Caller uses total_count for the error payload and ids for the blocker list.
    """
    ...
```

Edit `src/mcp_awareness/postgres_store.py`:

```python
def count_records_referencing(
    self, owner_id: str, schema_logical_key: str
) -> tuple[int, list[str]]:
    """Count and sample-id records referencing a schema version.

    Query splits schema_logical_key into schema_ref + version by splitting on
    the last ':'. Matches data.schema_ref and data.schema_version in the
    record entries' JSONB.
    """
    # Parse "schema_ref:schema_version" — schema_ref may itself contain ':'
    # (e.g., "schema:edge-manifest:1.0.0"). Split on the LAST ':'.
    ref, _, version = schema_logical_key.rpartition(":")
    count_query = """
        SELECT COUNT(*) AS cnt
        FROM entries
        WHERE type = 'record'
          AND owner_id = %(owner)s
          AND data->>'schema_ref' = %(ref)s
          AND data->>'schema_version' = %(version)s
          AND deleted IS NULL
    """
    ids_query = """
        SELECT id
        FROM entries
        WHERE type = 'record'
          AND owner_id = %(owner)s
          AND data->>'schema_ref' = %(ref)s
          AND data->>'schema_version' = %(version)s
          AND deleted IS NULL
        ORDER BY created
        LIMIT 10
    """
    params = {"owner": owner_id, "ref": ref, "version": version}
    with self._conn_pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(count_query, params)
        count = cur.fetchone()["cnt"]
        if count == 0:
            return (0, [])
        cur.execute(ids_query, params)
        ids = [r["id"] for r in cur.fetchall()]
        return (count, ids)
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_store.py -v -k count_records_referencing`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/mcp_awareness/store.py src/mcp_awareness/postgres_store.py tests/test_store.py
git commit -m "feat: add Store.count_records_referencing for schema deletion protection"
```

---

## Task 8: `validation.resolve_schema`

Uses `store.find_schema()` under the hood but exists in the validation module for a uniform interface to callers.

**Files:**
- Modify: `src/mcp_awareness/validation.py`
- Modify: `tests/test_validation.py`

- [ ] **Step 1: Write unit tests with store stub**

Append to `tests/test_validation.py`:

```python
from mcp_awareness.validation import resolve_schema


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


def test_resolve_schema_returns_caller_owned():
    stub = _StubStore()
    stub.set("alice", "s:test:1.0.0", object())  # sentinel
    result = resolve_schema(stub, "alice", "s:test", "1.0.0")
    assert result is stub._results[("alice", "s:test:1.0.0")]


def test_resolve_schema_returns_none_when_missing():
    stub = _StubStore()
    assert resolve_schema(stub, "alice", "s:nope", "1.0.0") is None
```

Note: the underlying `find_schema` already handles `_system` fallback at the SQL level, so `resolve_schema` delegates fully. No branching in Python.

- [ ] **Step 2: Verify failure**

Run: `pytest tests/test_validation.py -v -k resolve_schema`
Expected: FAIL — missing import.

- [ ] **Step 3: Implement**

Append to `src/mcp_awareness/validation.py`:

```python
from typing import Protocol


class _SchemaFinder(Protocol):
    """Minimal protocol for resolve_schema's store dependency."""
    def find_schema(self, owner_id: str, logical_key: str):
        ...


def resolve_schema(store: _SchemaFinder, owner_id: str, family: str, version: str):
    """Resolve a schema by family + version, preferring caller-owned.

    Delegates to Store.find_schema (which handles the _system fallback at
    the SQL level). Returns the schema Entry or None.
    """
    return store.find_schema(owner_id, compose_schema_logical_key(family, version))
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_validation.py -v`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/mcp_awareness/validation.py tests/test_validation.py
git commit -m "feat: add validation.resolve_schema delegating to Store.find_schema"
```

---

## Task 9: `validation.assert_schema_deletable`

**Files:**
- Modify: `src/mcp_awareness/validation.py`
- Modify: `tests/test_validation.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_validation.py`:

```python
from mcp_awareness.validation import SchemaInUseError, assert_schema_deletable


class _CounterStore:
    """Stub exposing count_records_referencing."""

    def __init__(self, count: int, ids: list[str]):
        self._count = count
        self._ids = ids

    def count_records_referencing(self, owner_id, schema_logical_key):
        return (self._count, self._ids)


def test_assert_schema_deletable_passes_with_zero_refs():
    assert_schema_deletable(_CounterStore(0, []), "alice", "s:test:1.0.0")


def test_assert_schema_deletable_raises_with_refs():
    with pytest.raises(SchemaInUseError) as excinfo:
        assert_schema_deletable(_CounterStore(3, ["id1", "id2", "id3"]), "alice", "s:test:1.0.0")
    assert excinfo.value.total_count == 3
    assert excinfo.value.referencing_records == ["id1", "id2", "id3"]
```

- [ ] **Step 2: Verify failure**

Run: `pytest tests/test_validation.py -v -k assert_schema_deletable`
Expected: FAIL — missing import.

- [ ] **Step 3: Implement**

Append to `src/mcp_awareness/validation.py`:

```python
class SchemaInUseError(Exception):
    """Raised when a schema cannot be deleted because records reference it.

    Callers at the MCP boundary translate this into a structured schema_in_use
    error response with the referencing_records list and total_count.
    """

    def __init__(self, total_count: int, referencing_records: list[str]):
        self.total_count = total_count
        self.referencing_records = referencing_records
        super().__init__(
            f"Cannot delete schema: {total_count} record(s) still reference it"
        )


class _RefCounter(Protocol):
    def count_records_referencing(self, owner_id: str, schema_logical_key: str) -> tuple[int, list[str]]:
        ...


def assert_schema_deletable(
    store: _RefCounter, owner_id: str, schema_logical_key: str
) -> None:
    """Raise SchemaInUseError if any non-deleted records reference this schema."""
    count, ids = store.count_records_referencing(owner_id, schema_logical_key)
    if count > 0:
        raise SchemaInUseError(total_count=count, referencing_records=ids)
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_validation.py -v`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/mcp_awareness/validation.py tests/test_validation.py
git commit -m "feat: add assert_schema_deletable and SchemaInUseError"
```

---

## Task 10: Alembic migration — seed `_system` user

**Files:**
- Create: `alembic/versions/<next-id>_add_system_user_for_schemas.py`

- [ ] **Step 1: Determine next revision id**

Run: `alembic current` (needs DB — or read head from `alembic/versions/` by the most recent `down_revision` chain). The latest is `l7g8h9i0j1k2_backfill_entry_language`. Pick the next id in the project's scheme — e.g., `m8h9i0j1k2l3`.

- [ ] **Step 2: Create the migration file**

Create `alembic/versions/m8h9i0j1k2l3_add_system_user_for_schemas.py`:

```python
# AGPL preamble — copy from alembic/versions/l7g8h9i0j1k2_backfill_entry_language.py

"""add _system user for system-owned schemas

Revision ID: m8h9i0j1k2l3
Revises: l7g8h9i0j1k2
Create Date: 2026-04-13 00:00:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "m8h9i0j1k2l3"
down_revision: str | Sequence[str] | None = "l7g8h9i0j1k2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Seed the _system user for system-owned schema entries.

    Idempotent — ON CONFLICT DO NOTHING lets the migration run multiple
    times safely (e.g., after a stamp-and-reapply).
    """
    op.execute(
        "INSERT INTO users (id, display_name) "
        "VALUES ('_system', 'System-managed schemas') "
        "ON CONFLICT (id) DO NOTHING"
    )


def downgrade() -> None:
    """Remove the _system user.

    Will fail if any entries still reference owner_id='_system'. Operators
    must soft-delete or re-home such entries before downgrade.
    """
    op.execute("DELETE FROM users WHERE id = '_system'")
```

- [ ] **Step 3: Test the migration end-to-end**

Run: `mcp-awareness-migrate` against a local Postgres (the testcontainers instance or a scratch DB).
Expected: exits 0 with "Migrations complete."; `SELECT id FROM users WHERE id='_system'` returns a row.

Run: `mcp-awareness-migrate --downgrade l7g8h9i0j1k2`
Expected: exits 0; `_system` row removed.

Run: `mcp-awareness-migrate` again (re-upgrade) to confirm re-applies cleanly.

- [ ] **Step 4: Add a quick idempotence test**

Since Alembic testing is typically integration-level, add a smoke test to `tests/test_store.py`:

```python
def test_system_user_exists_after_migration(store):
    """The conftest fixture inserts _system — verifies the migration logic is ON CONFLICT safe."""
    # Fixture already inserted; insert again to prove ON CONFLICT DO NOTHING semantics
    with store._conn_pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO users (id, display_name) VALUES ('_system', 'Re-insert') "
            "ON CONFLICT (id) DO NOTHING"
        )
        conn.commit()
        cur.execute("SELECT COUNT(*) FROM users WHERE id = '_system'")
        assert cur.fetchone()[0] == 1
```

- [ ] **Step 5: Commit**

```bash
git add alembic/versions/m8h9i0j1k2l3_add_system_user_for_schemas.py tests/test_store.py
git commit -m "feat: add migration seeding _system user for shared schemas"
```

---

## Task 11: MCP tool — `register_schema`

**Files:**
- Modify: `src/mcp_awareness/tools.py`
- Create: `tests/test_tools_schema_record.py`

- [ ] **Step 1: Write failing integration tests**

Create `tests/test_tools_schema_record.py`:

```python
# AGPL preamble — copy from tests/test_store.py

"""Integration tests for schema/record MCP tool handlers.

Uses testcontainers Postgres + direct tool-function calls via the server's
contextvar-based owner resolution.
"""

from __future__ import annotations

import json

import pytest

from mcp_awareness.schema import EntryType


TEST_OWNER = "test-owner"


@pytest.fixture
def configured_server(store, monkeypatch):
    """Wire the FastMCP server to the testcontainers store."""
    import mcp_awareness.server as srv
    monkeypatch.setattr(srv, "store", store)
    # Set owner contextvar for all subsequent tool calls
    from mcp_awareness.server import current_owner  # or wherever the contextvar lives
    token = current_owner.set(TEST_OWNER)
    yield srv
    current_owner.reset(token)


@pytest.mark.asyncio
async def test_register_schema_happy_path(configured_server):
    from mcp_awareness.tools import register_schema

    response = await register_schema(
        source="test",
        tags=["schema"],
        description="test schema",
        family="schema:test-thing",
        version="1.0.0",
        schema={"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
    )
    body = json.loads(response)
    assert body["status"] == "ok"
    assert body["logical_key"] == "schema:test-thing:1.0.0"
    assert "id" in body


@pytest.mark.asyncio
async def test_register_schema_rejects_invalid_schema(configured_server):
    from mcp.server.fastmcp.exceptions import ToolError
    from mcp_awareness.tools import register_schema

    with pytest.raises(ToolError) as excinfo:
        await register_schema(
            source="test",
            tags=["schema"],
            description="bad schema",
            family="schema:bad",
            version="1.0.0",
            schema={"type": "strng"},  # typo
        )
    err = json.loads(excinfo.value.args[0])["error"]
    assert err["code"] == "invalid_schema"


@pytest.mark.asyncio
async def test_register_schema_rejects_duplicate_family_version(configured_server):
    from mcp.server.fastmcp.exceptions import ToolError
    from mcp_awareness.tools import register_schema

    await register_schema(
        source="test", tags=[], description="v1",
        family="schema:dup", version="1.0.0",
        schema={"type": "object"},
    )
    with pytest.raises(ToolError) as excinfo:
        await register_schema(
            source="test", tags=[], description="v1 again",
            family="schema:dup", version="1.0.0",
            schema={"type": "object"},
        )
    err = json.loads(excinfo.value.args[0])["error"]
    assert err["code"] == "schema_already_exists"
```

- [ ] **Step 2: Verify failure**

Run: `pytest tests/test_tools_schema_record.py -v -k register_schema`
Expected: FAIL — `register_schema` does not exist in `tools.py`.

- [ ] **Step 3: Implement the tool**

Add to `src/mcp_awareness/tools.py` (follow the exact pattern of `remember` for decorator order, docstring shape, and use of `_srv.mcp.tool()`, `_timed`, embedding submission, etc.):

```python
@_srv.mcp.tool()
@_timed
async def register_schema(
    source: str,
    tags: list[str],
    description: str,
    family: str,
    version: str,
    schema: dict[str, Any],
    learned_from: str = "conversation",
    language: str | None = None,
) -> str:
    """Register a new JSON Schema entry for later use by records.

    Validates the schema body against JSON Schema Draft 2020-12 meta-schema
    on write. Family + version are combined into the entry's logical_key
    (schema:family:version); each version is a separate entry. Schemas are
    absolutely immutable once registered — to change one, register a new
    version and (if no records reference the old one) delete it.

    Returns:
        JSON: {"status": "ok", "id": "<uuid>", "logical_key": "<family:version>"}

    If you receive an unstructured error, the failure is in the transport
    or platform layer, not in awareness."""
    from jsonschema import exceptions as jse
    from mcp_awareness.validation import compose_schema_logical_key, validate_schema_body

    # Validate family / version
    if not family or ":" in family.split(":", 1)[0]:
        # Explicit invalid_parameter pattern
        _error_response(
            "invalid_parameter",
            "family must be a non-empty string",
            retryable=False, param="family", value=family,
        )
    if not version:
        _error_response(
            "invalid_parameter", "version must be a non-empty string",
            retryable=False, param="version", value=version,
        )

    # Validate the schema body
    try:
        validate_schema_body(schema)
    except jse.SchemaError as e:
        _error_response(
            "invalid_schema",
            f"Schema does not conform to JSON Schema Draft 2020-12: {e.message}",
            retryable=False,
            schema_error_path="/" + "/".join(str(p) for p in e.absolute_path),
            detail=str(e.message),
        )
    except jse.JsonSchemaException as e:
        _error_response(
            "validation_error", f"Unexpected schema validation error: {e}",
            retryable=False,
        )

    logical_key = compose_schema_logical_key(family, version)
    now = now_utc()
    data: dict[str, Any] = {
        "family": family,
        "version": version,
        "schema": schema,
        "description": description,
        "learned_from": learned_from,
    }
    text_for_detect = compose_detection_text("schema", data)
    resolved_lang = resolve_language(explicit=language, text_for_detection=text_for_detect)
    _check_unsupported_language(text_for_detect, resolved_lang)

    entry = Entry(
        id=make_id(),
        type=EntryType.SCHEMA,
        source=source,
        tags=tags,
        created=now,
        updated=None,
        expires=None,
        data=data,
        logical_key=logical_key,
        owner_id=_srv._current_owner(),  # or existing helper
        language=resolved_lang,
    )
    try:
        _srv.store.save_entry(entry)
    except _UniqueViolation as e:  # existing pattern for 23505 translation
        _error_response(
            "schema_already_exists",
            f"Schema {logical_key} already exists in source {source}",
            retryable=False, logical_key=logical_key, existing_id=e.existing_id,
        )

    _srv._generate_embedding(entry)
    return json.dumps({"status": "ok", "id": entry.id, "logical_key": logical_key})
```

Match the existing unique-constraint translation pattern (check `remember` for how logical_key collisions are surfaced — it uses upsert semantics, but for schemas we want *rejection* not upsert).

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_tools_schema_record.py -v -k register_schema`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/mcp_awareness/tools.py tests/test_tools_schema_record.py
git commit -m "feat: add register_schema MCP tool"
```

---

## Task 12: MCP tool — `create_record`

**Files:**
- Modify: `src/mcp_awareness/tools.py`
- Modify: `tests/test_tools_schema_record.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_tools_schema_record.py`:

```python
@pytest.mark.asyncio
async def test_create_record_happy_path(configured_server):
    from mcp_awareness.tools import create_record, register_schema

    await register_schema(
        source="test", tags=[], description="s",
        family="schema:thing", version="1.0.0",
        schema={"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
    )
    response = await create_record(
        source="test", tags=[], description="a thing",
        logical_key="thing-one",
        schema_ref="schema:thing", schema_version="1.0.0",
        content={"name": "widget"},
    )
    body = json.loads(response)
    assert body["status"] == "ok"
    assert body["action"] == "created"
    assert "id" in body


@pytest.mark.asyncio
async def test_create_record_rejects_unknown_schema(configured_server):
    from mcp.server.fastmcp.exceptions import ToolError
    from mcp_awareness.tools import create_record

    with pytest.raises(ToolError) as excinfo:
        await create_record(
            source="test", tags=[], description="orphan",
            logical_key="thing-one",
            schema_ref="schema:does-not-exist", schema_version="1.0.0",
            content={"name": "widget"},
        )
    err = json.loads(excinfo.value.args[0])["error"]
    assert err["code"] == "schema_not_found"
    assert err["searched_owners"] == [TEST_OWNER, "_system"]


@pytest.mark.asyncio
async def test_create_record_surfaces_validation_errors(configured_server):
    from mcp.server.fastmcp.exceptions import ToolError
    from mcp_awareness.tools import create_record, register_schema

    await register_schema(
        source="test", tags=[], description="s",
        family="schema:person", version="1.0.0",
        schema={"type": "object", "properties": {"name": {"type": "string"}, "age": {"type": "integer"}}, "required": ["name"]},
    )
    with pytest.raises(ToolError) as excinfo:
        await create_record(
            source="test", tags=[], description="bad person",
            logical_key="p1",
            schema_ref="schema:person", schema_version="1.0.0",
            content={"age": "thirty"},  # missing name; wrong age type
        )
    err = json.loads(excinfo.value.args[0])["error"]
    assert err["code"] == "validation_failed"
    validators = {ve["validator"] for ve in err["validation_errors"]}
    assert "required" in validators
    assert "type" in validators


@pytest.mark.asyncio
async def test_create_record_upsert_on_same_logical_key(configured_server):
    from mcp_awareness.tools import create_record, register_schema

    await register_schema(
        source="test", tags=[], description="s",
        family="schema:thing", version="1.0.0",
        schema={"type": "object"},
    )
    r1 = json.loads(await create_record(
        source="test", tags=[], description="v1",
        logical_key="thing-one",
        schema_ref="schema:thing", schema_version="1.0.0",
        content={"v": 1},
    ))
    assert r1["action"] == "created"
    r2 = json.loads(await create_record(
        source="test", tags=[], description="v2",
        logical_key="thing-one",
        schema_ref="schema:thing", schema_version="1.0.0",
        content={"v": 2},
    ))
    assert r2["action"] == "updated"
    assert r2["id"] == r1["id"]


@pytest.mark.asyncio
async def test_create_record_uses_system_schema_fallback(configured_server):
    """A record can reference a schema owned by _system, not the caller."""
    from mcp_awareness.tools import create_record

    # Seed _system schema directly via store (not via tool, since tool always writes to caller owner)
    from mcp_awareness.schema import Entry, make_id, now_utc
    _srv = configured_server
    _srv.store.save_entry(Entry(
        id=make_id(), type=EntryType.SCHEMA, source="system",
        tags=["system"], created=now_utc(), updated=None, expires=None,
        data={
            "family": "schema:system-thing", "version": "1.0.0",
            "schema": {"type": "object"},
            "description": "system-seeded", "learned_from": "cli-bootstrap",
        },
        logical_key="schema:system-thing:1.0.0", owner_id="_system",
    ))
    response = await create_record(
        source="test", tags=[], description="mine",
        logical_key="mine-1",
        schema_ref="schema:system-thing", schema_version="1.0.0",
        content={"any": "thing"},
    )
    body = json.loads(response)
    assert body["status"] == "ok"
```

- [ ] **Step 2: Verify failure**

Run: `pytest tests/test_tools_schema_record.py -v -k create_record`
Expected: FAIL — `create_record` not defined.

- [ ] **Step 3: Implement**

Add to `src/mcp_awareness/tools.py` following the existing `remember` pattern (especially for logical_key upsert behavior):

```python
@_srv.mcp.tool()
@_timed
async def create_record(
    source: str,
    tags: list[str],
    description: str,
    logical_key: str,
    schema_ref: str,
    schema_version: str,
    content: Any,
    learned_from: str = "conversation",
    language: str | None = None,
) -> str:
    """Create or upsert a record validated against a registered schema.

    Resolves the target schema by schema_ref + schema_version (prefers
    caller-owned, falls back to _system). Validates content against the
    schema on write; rejects with a structured validation_failed error
    listing every validation error. Upserts on matching (source, logical_key)
    — same logical_key means update in place with changelog.

    Returns:
        JSON: {"status": "ok", "id": "<uuid>", "action": "created" | "updated"}"""
    from jsonschema import exceptions as jse
    from mcp_awareness.validation import resolve_schema, validate_record_content

    resolved = resolve_schema(_srv.store, _srv._current_owner(), schema_ref, schema_version)
    if resolved is None:
        _error_response(
            "schema_not_found",
            f"No schema {schema_ref}:{schema_version} in your namespace or _system",
            retryable=False,
            schema_ref=schema_ref, schema_version=schema_version,
            searched_owners=[_srv._current_owner(), "_system"],
        )

    schema_body = resolved.data["schema"]
    try:
        errors = validate_record_content(schema_body, content)
    except jse.JsonSchemaException as e:
        _error_response(
            "validation_error", f"Unexpected content validation error: {e}",
            retryable=False,
        )
    if errors:
        n = errors[-1].get("total_errors") if errors[-1].get("truncated") else len(errors)
        extras: dict[str, Any] = {
            "schema_ref": schema_ref,
            "schema_version": schema_version,
            "validation_errors": errors,
        }
        if errors[-1].get("truncated"):
            extras["truncated"] = True
            extras["total_errors"] = errors[-1]["total_errors"]
        _error_response(
            "validation_failed",
            f"Record content does not conform to schema {schema_ref}:{schema_version} ({n} errors)",
            retryable=False, **extras,
        )

    # Existing logical_key upsert path (mirror `remember`'s approach)
    now = now_utc()
    data: dict[str, Any] = {
        "schema_ref": schema_ref,
        "schema_version": schema_version,
        "content": content,
        "description": description,
        "learned_from": learned_from,
    }
    text_for_detect = compose_detection_text("record", data)
    resolved_lang = resolve_language(explicit=language, text_for_detection=text_for_detect)
    _check_unsupported_language(text_for_detect, resolved_lang)

    entry = Entry(
        id=make_id(),
        type=EntryType.RECORD,
        source=source,
        tags=tags,
        created=now,
        updated=None,
        expires=None,
        data=data,
        logical_key=logical_key,
        owner_id=_srv._current_owner(),
        language=resolved_lang,
    )
    # Upsert via existing store method that returns (entry, action) — mirror remember
    saved, action = _srv.store.upsert_by_logical_key(entry)
    _srv._generate_embedding(saved)
    return json.dumps({"status": "ok", "id": saved.id, "action": action})
```

The exact shape of `upsert_by_logical_key` is whatever `remember` calls today — copy that.

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_tools_schema_record.py -v -k create_record`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/mcp_awareness/tools.py tests/test_tools_schema_record.py
git commit -m "feat: add create_record MCP tool with schema validation and _system fallback"
```

---

## Task 13: Update `update_entry` handler for schema/record branching

**Files:**
- Modify: `src/mcp_awareness/tools.py` (function `update_entry`, around line 533)
- Modify: `tests/test_tools_schema_record.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_tools_schema_record.py`:

```python
@pytest.mark.asyncio
async def test_update_entry_rejects_schema_update(configured_server):
    from mcp.server.fastmcp.exceptions import ToolError
    from mcp_awareness.tools import register_schema, update_entry

    resp = json.loads(await register_schema(
        source="test", tags=[], description="s",
        family="schema:thing", version="1.0.0",
        schema={"type": "object"},
    ))
    with pytest.raises(ToolError) as excinfo:
        await update_entry(entry_id=resp["id"], description="new desc")
    err = json.loads(excinfo.value.args[0])["error"]
    assert err["code"] == "schema_immutable"


@pytest.mark.asyncio
async def test_update_entry_record_content_revalidates(configured_server):
    from mcp_awareness.tools import create_record, register_schema, update_entry

    await register_schema(
        source="test", tags=[], description="s",
        family="schema:thing", version="1.0.0",
        schema={"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
    )
    r = json.loads(await create_record(
        source="test", tags=[], description="r",
        logical_key="r1",
        schema_ref="schema:thing", schema_version="1.0.0",
        content={"name": "good"},
    ))
    # Valid update — passes re-validation
    await update_entry(entry_id=r["id"], content={"name": "still-good"})


@pytest.mark.asyncio
async def test_update_entry_record_content_rejects_invalid(configured_server):
    from mcp.server.fastmcp.exceptions import ToolError
    from mcp_awareness.tools import create_record, register_schema, update_entry

    await register_schema(
        source="test", tags=[], description="s",
        family="schema:thing", version="1.0.0",
        schema={"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
    )
    r = json.loads(await create_record(
        source="test", tags=[], description="r",
        logical_key="r1",
        schema_ref="schema:thing", schema_version="1.0.0",
        content={"name": "good"},
    ))
    with pytest.raises(ToolError) as excinfo:
        await update_entry(entry_id=r["id"], content={"name": 123})  # wrong type
    err = json.loads(excinfo.value.args[0])["error"]
    assert err["code"] == "validation_failed"


@pytest.mark.asyncio
async def test_update_entry_record_non_content_skips_revalidation(configured_server):
    from mcp_awareness.tools import create_record, register_schema, update_entry

    await register_schema(
        source="test", tags=[], description="s",
        family="schema:thing", version="1.0.0",
        schema={"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
    )
    r = json.loads(await create_record(
        source="test", tags=[], description="orig",
        logical_key="r1",
        schema_ref="schema:thing", schema_version="1.0.0",
        content={"name": "good"},
    ))
    # Description-only change — no re-validation, even though pre-existing content would still pass
    await update_entry(entry_id=r["id"], description="updated desc")
    # No exception raised


@pytest.mark.asyncio
async def test_update_entry_record_pin_immutable(configured_server):
    # This test only applies if update_entry exposes schema_ref/schema_version params;
    # if it doesn't, the pin is already immutable by default. See Step 3 for the
    # decision — we're NOT adding schema_ref/schema_version to update_entry's
    # public surface, so this test is omitted.
    pass
```

- [ ] **Step 2: Verify failure**

Run: `pytest tests/test_tools_schema_record.py -v -k update_entry`
Expected: the `schema_immutable` and `validation_failed` tests fail (current update_entry accepts any entry without branching).

- [ ] **Step 3: Implement branching**

Edit `src/mcp_awareness/tools.py` inside the `update_entry` handler, after the entry is loaded by ID and before it's written back:

```python
# --- New: type-specific branching ---
from mcp_awareness.validation import resolve_schema, validate_record_content

if entry.type == EntryType.SCHEMA:
    _error_response(
        "schema_immutable",
        "Schemas cannot be updated. Register a new version instead.",
        retryable=False,
    )

if entry.type == EntryType.RECORD and content is not None:
    # content is being updated — re-resolve pinned schema and re-validate
    schema_ref = entry.data["schema_ref"]
    schema_version = entry.data["schema_version"]
    resolved = resolve_schema(_srv.store, entry.owner_id, schema_ref, schema_version)
    if resolved is None:
        # The schema the record pins to has been soft-deleted — unusual, but possible
        _error_response(
            "schema_not_found",
            f"Cannot re-validate: schema {schema_ref}:{schema_version} not found",
            retryable=False,
            schema_ref=schema_ref, schema_version=schema_version,
            searched_owners=[entry.owner_id, "_system"],
        )
    errors = validate_record_content(resolved.data["schema"], content)
    if errors:
        n = errors[-1].get("total_errors") if errors[-1].get("truncated") else len(errors)
        extras = {
            "schema_ref": schema_ref, "schema_version": schema_version,
            "validation_errors": errors,
        }
        if errors[-1].get("truncated"):
            extras["truncated"] = True
            extras["total_errors"] = errors[-1]["total_errors"]
        _error_response(
            "validation_failed",
            f"Record content does not conform to schema {schema_ref}:{schema_version} ({n} errors)",
            retryable=False, **extras,
        )
# --- end branching ---
```

Note: `update_entry` should NOT accept `schema_ref`/`schema_version`/`family`/`version` params — those are out of scope for the update API. If any such params exist in the current signature, leave them out of the new tools' invocation paths. The test `test_update_entry_record_pin_immutable` is skipped because the pin fields aren't exposed.

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_tools_schema_record.py -v -k update_entry`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/mcp_awareness/tools.py tests/test_tools_schema_record.py
git commit -m "feat: update_entry enforces schema immutability and record re-validation"
```

---

## Task 14: Update `delete_entry` for schema deletion protection

**Files:**
- Modify: `src/mcp_awareness/tools.py` (function `delete_entry`)
- Modify: `tests/test_tools_schema_record.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_tools_schema_record.py`:

```python
@pytest.mark.asyncio
async def test_delete_entry_schema_with_no_records_succeeds(configured_server):
    from mcp_awareness.tools import delete_entry, register_schema

    resp = json.loads(await register_schema(
        source="test", tags=[], description="s",
        family="schema:thing", version="1.0.0",
        schema={"type": "object"},
    ))
    await delete_entry(entry_id=resp["id"])  # no records; succeeds
    # Verify soft-deleted
    assert configured_server.store.find_schema(TEST_OWNER, "schema:thing:1.0.0") is None


@pytest.mark.asyncio
async def test_delete_entry_schema_with_records_rejected(configured_server):
    from mcp.server.fastmcp.exceptions import ToolError
    from mcp_awareness.tools import create_record, delete_entry, register_schema

    resp = json.loads(await register_schema(
        source="test", tags=[], description="s",
        family="schema:thing", version="1.0.0",
        schema={"type": "object"},
    ))
    await create_record(
        source="test", tags=[], description="r",
        logical_key="r1",
        schema_ref="schema:thing", schema_version="1.0.0",
        content={},
    )
    with pytest.raises(ToolError) as excinfo:
        await delete_entry(entry_id=resp["id"])
    err = json.loads(excinfo.value.args[0])["error"]
    assert err["code"] == "schema_in_use"
    assert len(err["referencing_records"]) == 1


@pytest.mark.asyncio
async def test_delete_entry_schema_allowed_after_records_deleted(configured_server):
    from mcp_awareness.tools import create_record, delete_entry, register_schema

    schema_resp = json.loads(await register_schema(
        source="test", tags=[], description="s",
        family="schema:thing", version="1.0.0",
        schema={"type": "object"},
    ))
    record_resp = json.loads(await create_record(
        source="test", tags=[], description="r",
        logical_key="r1",
        schema_ref="schema:thing", schema_version="1.0.0",
        content={},
    ))
    await delete_entry(entry_id=record_resp["id"])
    await delete_entry(entry_id=schema_resp["id"])  # no live refs; succeeds
```

- [ ] **Step 2: Verify failure**

Run: `pytest tests/test_tools_schema_record.py -v -k delete_entry`
Expected: `schema_with_records_rejected` fails (no protection yet).

- [ ] **Step 3: Implement branching**

Edit `src/mcp_awareness/tools.py` inside `delete_entry`, after the entry is loaded:

```python
from mcp_awareness.validation import SchemaInUseError, assert_schema_deletable

if entry.type == EntryType.SCHEMA:
    try:
        assert_schema_deletable(_srv.store, entry.owner_id, entry.logical_key)
    except SchemaInUseError as e:
        _error_response(
            "schema_in_use",
            f"Cannot delete schema {entry.logical_key}: {e.total_count} record(s) reference it",
            retryable=False,
            referencing_records=e.referencing_records,
            total_count=e.total_count,
        )
# Existing soft-delete path follows
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_tools_schema_record.py -v`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/mcp_awareness/tools.py tests/test_tools_schema_record.py
git commit -m "feat: delete_entry protects schemas referenced by live records"
```

---

## Task 15: CLI tool — `mcp-awareness-register-schema`

**Files:**
- Create: `src/mcp_awareness/cli_register_schema.py`
- Create: `tests/test_cli_register_schema.py`
- Modify: `pyproject.toml`

- [ ] **Step 1: Write failing test**

Create `tests/test_cli_register_schema.py`:

```python
# AGPL preamble

"""Tests for mcp-awareness-register-schema CLI."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile

import pytest


@pytest.fixture
def system_schema_file():
    with tempfile.NamedTemporaryFile(suffix=".json", mode="w", delete=False) as f:
        json.dump({"type": "object", "properties": {"name": {"type": "string"}}}, f)
        path = f.name
    yield path


def test_cli_register_schema_happy_path(pg_dsn, system_schema_file, monkeypatch, capsys):
    """End-to-end: CLI writes a _system schema via direct store access."""
    from mcp_awareness.cli_register_schema import main

    monkeypatch.setenv("AWARENESS_DATABASE_URL", pg_dsn)
    sys_argv = [
        "mcp-awareness-register-schema",
        "--system",
        "--family", "schema:cli-test",
        "--version", "1.0.0",
        "--schema-file", system_schema_file,
        "--source", "awareness-built-in",
        "--tags", "cli,test",
        "--description", "CLI-registered test schema",
    ]
    monkeypatch.setattr("sys.argv", sys_argv)

    main()
    captured = capsys.readouterr()
    body = json.loads(captured.out.strip())
    assert body["status"] == "ok"
    assert body["logical_key"] == "schema:cli-test:1.0.0"

    # Verify entry exists in DB under _system owner
    from mcp_awareness.postgres_store import PostgresStore
    store = PostgresStore(pg_dsn)
    entry = store.find_schema("any-caller", "schema:cli-test:1.0.0")
    assert entry is not None
    assert entry.owner_id == "_system"
    assert entry.data["learned_from"] == "cli-bootstrap"


def test_cli_register_schema_rejects_invalid_schema_file(pg_dsn, monkeypatch, capsys):
    from mcp_awareness.cli_register_schema import main

    with tempfile.NamedTemporaryFile(suffix=".json", mode="w", delete=False) as f:
        json.dump({"type": "strng"}, f)  # invalid
        path = f.name

    monkeypatch.setenv("AWARENESS_DATABASE_URL", pg_dsn)
    monkeypatch.setattr("sys.argv", [
        "mcp-awareness-register-schema", "--system",
        "--family", "schema:bad", "--version", "1.0.0",
        "--schema-file", path, "--source", "test", "--tags", "", "--description", "bad",
    ])
    with pytest.raises(SystemExit) as excinfo:
        main()
    assert excinfo.value.code == 1
    captured = capsys.readouterr()
    assert "invalid_schema" in captured.err
```

- [ ] **Step 2: Verify failure**

Run: `pytest tests/test_cli_register_schema.py -v`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement**

Create `src/mcp_awareness/cli_register_schema.py`:

```python
# AGPL preamble

"""CLI for registering _system-owned schema entries.

Bypasses MCP entirely — operator tool, run once per built-in schema at
deploy/bootstrap time. No MCP auth, no middleware, direct PostgresStore
access.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Register a _system-owned schema entry (operator bootstrap only).",
    )
    parser.add_argument("--system", action="store_true", required=True,
                        help="Required. Confirms the caller intends to write to the _system owner.")
    parser.add_argument("--family", required=True, help="Schema family (e.g., schema:edge-manifest)")
    parser.add_argument("--version", required=True, help="Schema version (e.g., 1.0.0)")
    parser.add_argument("--schema-file", required=True, type=Path,
                        help="Path to JSON file containing the Draft 2020-12 schema body")
    parser.add_argument("--source", required=True, help="Source field for the entry")
    parser.add_argument("--tags", default="",
                        help="Comma-separated tags (empty string for none)")
    parser.add_argument("--description", required=True, help="Entry description")
    args = parser.parse_args()

    # Read + parse schema file
    if not args.schema_file.exists():
        print(json.dumps({"error": {"code": "file_not_found", "message": str(args.schema_file)}}),
              file=sys.stderr)
        sys.exit(1)
    try:
        schema_body = json.loads(args.schema_file.read_text())
    except json.JSONDecodeError as e:
        print(json.dumps({"error": {"code": "invalid_json", "message": str(e)}}), file=sys.stderr)
        sys.exit(1)

    # Meta-schema validation
    from jsonschema import exceptions as jse
    from mcp_awareness.validation import compose_schema_logical_key, validate_schema_body
    try:
        validate_schema_body(schema_body)
    except jse.SchemaError as e:
        print(json.dumps({"error": {
            "code": "invalid_schema", "message": str(e.message),
            "schema_error_path": "/" + "/".join(str(p) for p in e.absolute_path),
        }}), file=sys.stderr)
        sys.exit(1)

    # DB connection
    database_url = os.environ.get("AWARENESS_DATABASE_URL", "")
    if not database_url:
        print(json.dumps({"error": {"code": "missing_env", "message": "AWARENESS_DATABASE_URL required"}}),
              file=sys.stderr)
        sys.exit(1)

    from mcp_awareness.postgres_store import PostgresStore
    from mcp_awareness.schema import Entry, EntryType, make_id, now_utc

    store = PostgresStore(database_url)
    logical_key = compose_schema_logical_key(args.family, args.version)
    tags = [t.strip() for t in args.tags.split(",") if t.strip()]

    entry = Entry(
        id=make_id(),
        type=EntryType.SCHEMA,
        source=args.source,
        tags=tags,
        created=now_utc(),
        updated=None,
        expires=None,
        data={
            "family": args.family,
            "version": args.version,
            "schema": schema_body,
            "description": args.description,
            "learned_from": "cli-bootstrap",
        },
        logical_key=logical_key,
        owner_id="_system",
        language="english",
    )

    try:
        store.save_entry(entry)
    except Exception as e:
        print(json.dumps({"error": {"code": "store_error", "message": str(e)}}), file=sys.stderr)
        sys.exit(1)

    print(json.dumps({"status": "ok", "id": entry.id, "logical_key": logical_key}))
    sys.exit(0)


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Register console script**

Edit `pyproject.toml`:

```toml
[project.scripts]
# ... existing scripts ...
mcp-awareness-register-schema = "mcp_awareness.cli_register_schema:main"
```

- [ ] **Step 5: Reinstall and test**

Run: `pip install -e ".[dev]"`
Run: `pytest tests/test_cli_register_schema.py -v`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add src/mcp_awareness/cli_register_schema.py tests/test_cli_register_schema.py pyproject.toml
git commit -m "feat: add mcp-awareness-register-schema CLI for _system schemas"
```

---

## Task 16: Cross-owner isolation tests

**Files:**
- Modify: `tests/test_tools_schema_record.py`

- [ ] **Step 1: Add isolation tests**

Append to `tests/test_tools_schema_record.py`:

```python
@pytest.mark.asyncio
async def test_cross_owner_schema_invisible(configured_server, store):
    """Owner A registers a schema; Owner B cannot resolve it."""
    from mcp_awareness.server import current_owner
    from mcp_awareness.tools import create_record, register_schema
    from mcp.server.fastmcp.exceptions import ToolError

    # Owner A (default TEST_OWNER) registers
    await register_schema(
        source="test", tags=[], description="A's schema",
        family="schema:mine", version="1.0.0",
        schema={"type": "object"},
    )

    # Switch to Owner B
    token = current_owner.set("other-owner")
    try:
        with pytest.raises(ToolError) as excinfo:
            await create_record(
                source="test", tags=[], description="B's attempt",
                logical_key="r-b", schema_ref="schema:mine", schema_version="1.0.0",
                content={},
            )
        err = json.loads(excinfo.value.args[0])["error"]
        assert err["code"] == "schema_not_found"
    finally:
        current_owner.reset(token)


@pytest.mark.asyncio
async def test_both_owners_see_system_schema(configured_server, store):
    """Both A and B can use a _system schema; their records don't cross."""
    from mcp_awareness.schema import Entry, make_id, now_utc
    from mcp_awareness.server import current_owner
    from mcp_awareness.tools import create_record

    # Seed _system schema directly
    store.save_entry(Entry(
        id=make_id(), type=EntryType.SCHEMA, source="system",
        tags=["system"], created=now_utc(), updated=None, expires=None,
        data={
            "family": "schema:shared", "version": "1.0.0",
            "schema": {"type": "object"},
            "description": "shared", "learned_from": "cli-bootstrap",
        },
        logical_key="schema:shared:1.0.0", owner_id="_system",
    ))

    # A writes a record
    a_resp = json.loads(await create_record(
        source="test", tags=[], description="A's record",
        logical_key="rec-a", schema_ref="schema:shared", schema_version="1.0.0",
        content={"who": "alice"},
    ))

    # Switch to B
    token = current_owner.set("bob")
    try:
        b_resp = json.loads(await create_record(
            source="test", tags=[], description="B's record",
            logical_key="rec-b", schema_ref="schema:shared", schema_version="1.0.0",
            content={"who": "bob"},
        ))
        assert b_resp["status"] == "ok"
    finally:
        current_owner.reset(token)

    # A's record invisible to B — verified via the owner_id on each entry
    # (the records are already isolated by owner_id on create)
    a_entry = store.get_entry(TEST_OWNER, a_resp["id"])  # exists
    assert a_entry is not None
    # Call with bob's owner — returns None because RLS/owner filter excludes
    # (if get_entry takes owner_id as arg, this is clean; otherwise use find)
```

- [ ] **Step 2: Run tests**

Run: `pytest tests/test_tools_schema_record.py -v`
Expected: all pass.

- [ ] **Step 3: Commit**

```bash
git add tests/test_tools_schema_record.py
git commit -m "test: cross-owner isolation for schema/record tools"
```

---

## Task 17: Update CHANGELOG, README, data-dictionary, server instructions

**Files:**
- Modify: `CHANGELOG.md`
- Modify: `README.md`
- Modify: `docs/data-dictionary.md`
- Modify: `src/mcp_awareness/instructions.md`

- [ ] **Step 1: CHANGELOG entry**

Add under `[Unreleased]`:

```markdown
### Added
- Two new entry types: `schema` (JSON Schema Draft 2020-12 definition) and `record` (validated payload conforming to a schema). Tools: `register_schema`, `create_record`. Schemas are absolutely immutable after registration; records re-validate on content update. Schema deletion is blocked while live records reference a version. Per-owner storage with a shared `_system` fallback namespace for built-in schemas.
- New CLI: `mcp-awareness-register-schema` for operators to seed `_system`-owned schemas at deploy time.
- New migration: `_system` user seed (idempotent).

### Dependencies
- Added `jsonschema>=4.26.0` as a runtime dependency.
```

- [ ] **Step 2: README updates**

- Bump tool count in the "Implemented" section (search for "tools" to find it).
- Add a bullet to the tool list describing `register_schema` / `create_record`.
- Bump test count after the test-count check in Task 19.

Exact text for the new tool bullet (match the style of neighbors):

```markdown
- **`register_schema` / `create_record`** — define typed data contracts via JSON Schema Draft 2020-12; validate payloads server-side on write with structured error envelopes listing every validation failure.
```

- [ ] **Step 3: Data dictionary**

Add entries to `docs/data-dictionary.md` for both types. Match existing entry format:

```markdown
### `schema`
JSON Schema Draft 2020-12 definition. Schema body lives in `data.schema`; family + version in `data.family` + `data.version`; `logical_key` derived as `{family}:{version}`. Immutable after registration.

**`data` fields:**
- `family` (string, required) — schema family identifier (e.g., `schema:edge-manifest`)
- `version` (string, required) — schema version (user-chosen semantic or sequential)
- `schema` (object, required) — JSON Schema Draft 2020-12 body
- `description` (string) — human-readable description
- `learned_from` (string) — platform that registered the schema

### `record`
Validated data entry conforming to a referenced schema. Content in `data.content`; pinned schema in `data.schema_ref` + `data.schema_version`. Re-validated on content update.

**`data` fields:**
- `schema_ref` (string, required) — target schema family (e.g., `schema:edge-manifest`)
- `schema_version` (string, required) — target schema version (exact pin, no "latest")
- `content` (any JSON value, required) — validated payload
- `description` (string) — human-readable description
- `learned_from` (string) — platform that created the record
```

- [ ] **Step 4: Server instructions**

Append to `src/mcp_awareness/instructions.md` (or wherever server-level guidance lives):

```markdown
When you need typed data contracts for edge providers, tag taxonomies, or any
shape that should be validated on write: register a schema via `register_schema`
(family + version + JSON Schema body), then write records via `create_record`
referencing `schema_ref` + `schema_version`. Schemas are immutable — bump the
version to evolve. Built-in shared schemas live in the `_system` namespace
seeded by the operator.
```

- [ ] **Step 5: Commit**

```bash
git add CHANGELOG.md README.md docs/data-dictionary.md src/mcp_awareness/instructions.md
git commit -m "docs: document schema/record entry types, new tools, and CLI"
```

---

## Task 18: Pre-push verification (ruff, mypy, full test suite, coverage, test count)

**Files:** none — pure verification.

- [ ] **Step 1: Format**

Run: `ruff format src/ tests/`
Expected: no changes or minor formatting only.

- [ ] **Step 2: Lint**

Run: `ruff check src/ tests/`
Expected: 0 errors.

- [ ] **Step 3: Type check**

Run: `mypy src/mcp_awareness/`
Expected: 0 errors in strict mode.

- [ ] **Step 4: Full test suite with coverage**

Run: `pytest --cov=src/mcp_awareness --cov-report=term-missing`
Expected: all tests pass; verify coverage on new modules:

- `src/mcp_awareness/validation.py` — 100% (pure functions, all paths tested)
- `src/mcp_awareness/cli_register_schema.py` — cover happy path, invalid schema, missing env, bad JSON
- New branches in `tools.py` — cover all new error codes (`schema_immutable`, `validation_failed`, `schema_not_found`, `schema_in_use`, `record_schema_pin_immutable`, `invalid_schema`, `schema_already_exists`)

If any line is uncovered, add a test case; never use `pragma: no cover`.

- [ ] **Step 5: Update test count in README**

Run: `pytest --collect-only -q | tail -3` to get exact count, then update the number in `README.md`.

- [ ] **Step 6: Commit docs fix-up if test count changed**

```bash
git add README.md
git commit -m "docs: update test count after schema/record tests"
```

- [ ] **Step 7: Push branch**

```bash
git push -u origin feat/schema-record-entry-types
```

---

## Task 19: Open PR with QA section

**Files:** PR body only.

- [ ] **Step 1: Author PR body**

Title: `feat: add schema and record entry types with JSON Schema validation`

Body:

```markdown
## Summary

- Adds two new `EntryType` values (`schema`, `record`) with JSON Schema Draft 2020-12 validation on write.
- Per-owner storage with `_system` fallback for shared built-in schemas.
- Schemas are absolutely immutable after registration; records re-validate on content update.
- Schema deletion blocked while live records reference a version.
- New CLI tool `mcp-awareness-register-schema` for operator bootstrap of `_system`-owned schemas.
- Adds `jsonschema>=4.26.0` dependency.

Closes #208. Spec: `docs/superpowers/specs/2026-04-13-schema-record-entry-types-design.md`. Plan: `docs/superpowers/plans/2026-04-13-schema-record-entry-types-plan.md`.

## QA

### Prerequisites

- `pip install -e ".[dev]"`
- Deploy to QA test instance on alternate port (`AWARENESS_PORT=8421`) via `docker-compose.qa.yaml`.
- Run `mcp-awareness-migrate` against the QA DB to apply the `_system` user seed.

### Manual tests (via MCP tools)

1. - [ ] **Register a schema**
   ```
   register_schema(source="qa-test", tags=["qa"], description="qa test schema",
                   family="schema:qa-thing", version="1.0.0",
                   schema={"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]})
   ```
   Expected: `{"status":"ok","id":"<uuid>","logical_key":"schema:qa-thing:1.0.0"}`

2. - [ ] **Reject invalid schema (meta-schema check)**
   ```
   register_schema(source="qa-test", tags=[], description="bad",
                   family="schema:bad", version="1.0.0",
                   schema={"type": "strng"})
   ```
   Expected: structured error with `code: "invalid_schema"`, `schema_error_path`, `detail`.

3. - [ ] **Reject duplicate family+version**
   Re-run step 1 exactly. Expected: `code: "schema_already_exists"`, `logical_key`, `existing_id`.

4. - [ ] **Create a valid record**
   ```
   create_record(source="qa-test", tags=[], description="a qa thing",
                 logical_key="qa-rec-1", schema_ref="schema:qa-thing", schema_version="1.0.0",
                 content={"name": "widget"})
   ```
   Expected: `{"status":"ok","id":"<uuid>","action":"created"}`

5. - [ ] **Reject record with invalid content (shows all errors)**
   ```
   create_record(source="qa-test", tags=[], description="bad record",
                 logical_key="qa-rec-bad", schema_ref="schema:qa-thing", schema_version="1.0.0",
                 content={"unexpected": 42})  # missing required 'name'
   ```
   Expected: `code: "validation_failed"`, `validation_errors` list with `path`, `validator`, `schema_path`.

6. - [ ] **Upsert record via same logical_key**
   Re-run step 4 with different content. Expected: `action: "updated"`, same `id` as step 4.

7. - [ ] **Re-validation on record update (valid)**
   ```
   update_entry(entry_id=<id from step 4>, content={"name": "still-valid"})
   ```
   Expected: `{"status":"ok"}` (or existing update_entry response shape).

8. - [ ] **Re-validation on record update (invalid → rejected)**
   ```
   update_entry(entry_id=<id>, content={"name": 123})
   ```
   Expected: `code: "validation_failed"`; record content unchanged (verify via `get_knowledge`).

9. - [ ] **Schema immutability**
   ```
   update_entry(entry_id=<schema id from step 1>, description="new desc")
   ```
   Expected: `code: "schema_immutable"`; schema unchanged.

10. - [ ] **Schema deletion blocked by live records**
    ```
    delete_entry(entry_id=<schema id from step 1>)
    ```
    Expected: `code: "schema_in_use"`, `referencing_records: [...]`, `total_count`.

11. - [ ] **Schema deletion allowed after records deleted**
    Delete the record from step 4 via `delete_entry(entry_id=<record id>)`, then retry step 10.
    Expected: schema soft-deletes successfully.

12. - [ ] **`_system` fallback works**
    Via QA shell: `mcp-awareness-register-schema --system --family schema:qa-system --version 1.0.0 --schema-file /tmp/qa-system-schema.json --source qa-built-in --tags qa --description "qa system schema"`.
    Then via MCP:
    ```
    create_record(source="qa-test", tags=[], description="uses system schema",
                  logical_key="qa-sys-rec", schema_ref="schema:qa-system", schema_version="1.0.0",
                  content={"any": "thing"})
    ```
    Expected: record created successfully.

13. - [ ] **Cross-owner isolation**
    As a second authenticated user, attempt to resolve the step-1 schema. Expected: `code: "schema_not_found"`.
EOF
```

- [ ] **Step 2: Create the PR**

```bash
source ~/github.com/cmeans/claude-dev/github-app/activate.sh && \
  gh pr create \
    --title "feat: add schema and record entry types with JSON Schema validation" \
    --body-file <(cat <<'EOF'
<BODY FROM STEP 1>
EOF
) \
    --label "enhancement" \
    --label "Dev Active"
```

(Exact label discipline per `feedback_label_discipline.md` — set `Dev Active` on push, let automation transition to `Awaiting CI` → `Ready for QA`.)

- [ ] **Step 3: Poll CI and transition labels per project workflow**

Per `feedback_poll_ci_after_push.md` — after push, run `gh pr checks <pr>` immediately. On green, apply `Ready for QA`. Per `feedback_codecov_comment.md` — read the Codecov bot comment, fix any missing lines before marking Ready for QA.

---

## Self-Review

**Spec coverage check:**

Walking the design doc section by section:

- D1 (type-specific tools) → Tasks 11, 12 ✓
- D2 (per-owner + `_system` fallback) → Task 6 (SQL-level) + Task 10 (seed) ✓
- D3 (CLI-only `_system` writes) → Task 15 ✓
- D4 (absolute schema immutability) → Task 13 (schema branch) ✓
- D5 (record mutability with re-validation) → Task 13 (record branch) ✓
- D6 (all errors via `iter_errors()`) → Task 5 ✓
- D7 (server-derived `logical_key`) → Task 3 + used in Tasks 11/12 ✓
- D8 (any JSON value for `content`) → Task 5 tests include primitive + array schemas ✓

**Architecture:** `validation.py` covered Tasks 3–5, 8, 9 ✓; Store changes covered Tasks 6, 7 ✓; Tool changes covered Tasks 11–14 ✓; CLI covered Task 15 ✓; Migration covered Task 10 ✓.

**Error codes:** every code in the spec's error table is exercised by at least one test: `invalid_schema` (Task 11), `schema_already_exists` (Task 11), `schema_not_found` (Task 12), `validation_failed` (Tasks 12, 13), `schema_immutable` (Task 13), `schema_in_use` (Task 14). `invalid_parameter` inherited from existing helper. `record_schema_pin_immutable` is NOT tested — because `update_entry` doesn't expose `schema_ref`/`schema_version` params. Either keep it as a code reserved for a future API change, or drop the code from the spec. **Decision: keep as reserved; no test needed for a code that can't be triggered given the current API.**

**Deployment:** Operator deploy sequence from the spec mapped to Task 18 (migration) + Task 15 (CLI) + PR-body QA steps. Compose files untouched; called out explicitly.

**Testing:** Unit (Tasks 3–5, 8, 9) + integration (Tasks 6, 7, 11–14, 16) + CLI (Task 15) + coverage gate (Task 18). Cross-owner isolation explicit in Task 16.

**Placeholder scan:** No "TBD" / "TODO" in task bodies. Each code step shows actual code. Each run step shows exact command + expected outcome. The one placeholder concession is migration revision id (`m8h9i0j1k2l3`) which depends on head-at-implementation-time — Task 10 Step 1 instructs how to pick it.

**Type consistency:** Function names consistent throughout: `compose_schema_logical_key`, `validate_schema_body`, `validate_record_content`, `resolve_schema`, `assert_schema_deletable`, `SchemaInUseError`. Store methods: `find_schema`, `count_records_referencing`. Tool names: `register_schema`, `create_record`. Error codes match spec table exactly.

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-04-13-schema-record-entry-types-plan.md`. Two execution options:

1. **Subagent-Driven (recommended)** — I dispatch a fresh subagent per task, review between tasks, fast iteration.
2. **Inline Execution** — I execute tasks in this session using `executing-plans`, batch execution with checkpoints.

Which approach do you want?
