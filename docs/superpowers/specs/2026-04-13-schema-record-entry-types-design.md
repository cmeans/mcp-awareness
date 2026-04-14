# Schema + Record entry types — Design Spec

**Date:** 2026-04-13
**Issue:** [#208](https://github.com/cmeans/mcp-awareness/issues/208)
**Related awareness entries:** `design-schema-record-secrets` (`53b378b2`), intention `3117644f`
**Scope:** Implementation steps 1–2 of the awareness-edge prerequisite design. Secrets infrastructure (step 3+) is a separate follow-up.

## Problem

mcp-awareness stores arbitrary agent-written entries. Edge providers and the future tag taxonomy layer need *typed data contracts* — schemas that define a shape, and records that conform to those shapes with server-side validation on write. Without this, the entire edge config pattern (manifests, provider preferences, target configs) rests on implicit naming conventions with no validation — typos silently fall through to defaults, flagged as the #1 practical pain point in the edge design review.

This spec defines two new `EntryType` values — `schema` and `record` — with JSON Schema Draft 2020-12 validation on write, plus a system-owner fallback so canonical shared schemas can ship with the server.

## Goals

- Agents can register schemas via MCP and write records validated against them.
- Server-side enforcement: invalid schemas never stored; invalid records never stored.
- Canonical schemas (edge-manifest, edge-identity, eventually tag taxonomy) can live in a shared `_system` namespace, with per-user schemas as an override layer.
- Structured error responses listing *all* validation failures in one round trip.
- No new tool surface beyond two type-specific write tools; existing `update_entry` / `delete_entry` absorb the new types.

## Non-goals

- Secrets (`x-secret` encryption, one-time token web form, edge decrypt endpoint) — separate follow-up PR.
- Admin-via-MCP authorization (`is_admin` column on users) — deferred until actually needed.
- Cross-schema `$ref` resolution via `referencing.Registry` — deferred until a real use case demands it.
- Validator caching by schema version — deferred until throughput data justifies it.
- Backwards-compatibility shims for the historical "Structure"/"Structured"/"Secret" naming — superseded names; new implementation uses `schema`/`record`/`x-secret`.

## Design decisions

### D1. Tool surface: type-specific write tools

Two new MCP tools — `register_schema` and `create_record` — matching the existing convention of one type-specific tool per writable entry type (`remember` → note, `learn_pattern` → pattern, etc.). The MCP Bench audit flagged the 29-tool surface as bloated, but extending `remember` with polymorphic `entry_type` would muddy its semantic. A future PR may unify all write tools behind a generic `create_entry`; that is a separate refactor across existing tools, not scope for this work.

### D2. Multi-tenancy: per-owner with `_system` fallback

Schemas are scoped by `owner_id`. A reserved `_system` owner holds shared canonical schemas. Schema lookup queries `WHERE logical_key=? AND owner_id IN (caller, '_system') ORDER BY CASE WHEN owner_id=caller THEN 0 ELSE 1 END LIMIT 1` — caller's own schema wins over `_system` when both exist, giving operators predictable override semantics.

### D3. `_system` write mechanism: CLI only

A new console script `mcp-awareness-register-schema --system ...` writes `_system`-owned schemas, bypassing MCP. Operators (DB access + server config) seed built-in schemas at deploy/bootstrap time. No `is_admin` column, no MCP authz plumbing — bootstrap is a deploy-time concern, not agent-accessible.

### D4. Schema immutability: absolute

`update_entry` on a `schema` entry always returns `schema_immutable`. To change a schema, register a new version; if the old version has no non-deleted records, soft-delete it. Matches the spec's "new version = new entry" framing and removes state-dependent authoring behavior.

### D5. Record mutability: re-validated on content change

`update_entry` on a `record` entry re-resolves the pinned schema and re-validates `content` on update. Updates that fail re-validation are rejected; the record is left unchanged. Non-content field updates (tags, description, source) skip re-validation. `schema_ref` and `schema_version` are immutable on records — records pin to an exact schema version and cannot be re-targeted.

### D6. Validation error reporting: all errors via `iter_errors()`

The `validation_failed` envelope includes a `validation_errors` list with one entry per `iter_errors()` yield, sorted by `path`. Each entry has `path` (from `ValidationError.json_path`), `message`, `validator` (the failing JSON Schema keyword), and `schema_path`. Truncated at 50 errors with `truncated: true, total_errors: <n>` if more.

### D7. `logical_key` derivation: server-side

For schemas, the caller passes `family` and `version`; the server derives `logical_key = f"{family}:{version}"`. Single source of truth; impossible to end up with a mismatch. Records mirror the derivation on lookup: `resolve_schema` composes the target `logical_key` from the record's `schema_ref` + `schema_version`.

### D8. Record `content`: any JSON value

`data.content` on record entries accepts any JSON-serializable value (dict, list, primitive, null) — matches JSON Schema's ability to validate any value, and matches the existing polymorphic `content` parameter on `remember`. Ruling it out now would create a future migration for no real benefit.

## Architecture

### New module: `src/mcp_awareness/validation.py`

Pure functions, no I/O side effects except the store-lookup helper. Keeps `jsonschema` out of the store layer (preserves Store protocol as swappable) and makes validation unit-testable without Postgres.

| Function | Purpose |
|---|---|
| `validate_schema_body(schema: dict) -> None` | `Draft202012Validator.check_schema(schema)`. Translates `SchemaError` into structured `invalid_schema` error. |
| `resolve_schema(store, owner_id, family, version) -> Entry \| None` | Caller-owner lookup first, `_system` fallback. Excludes soft-deleted. |
| `validate_record_content(schema_body: dict, content: Any) -> list[dict]` | Runs `iter_errors()`, returns sorted list of error dicts. Empty list = valid. |
| `compose_schema_logical_key(family: str, version: str) -> str` | Single place the format lives: `f"{family}:{version}"`. |
| `assert_schema_deletable(store, owner_id, logical_key) -> None` | Queries referencing records. Raises `schema_in_use` with blocker list if any. |
| `collect_validation_errors(validator, instance) -> list[dict]` | Internal helper; handles truncation at 50. |

### Store protocol changes (`src/mcp_awareness/store.py`, `postgres_store.py`)

Two new methods on the `Store` protocol:

- `find_schema(owner_id: str, logical_key: str) -> Entry | None` — single-query schema lookup honoring the `_system` fallback and soft-delete exclusion.
- `count_records_referencing(owner_id: str, schema_logical_key: str) -> tuple[int, list[str]]` — supports schema-delete protection. Returns total count and up to N (default 10) referencing record IDs for the error envelope.

Existing `save_entry` / write paths absorb the new entry types unchanged — the `type` field is a TEXT enum value change, not a structural change.

### Tool surface changes (`src/mcp_awareness/tools.py`)

- **New:** `register_schema(source, tags, description, family, version, schema, learned_from="conversation") -> str`
- **New:** `create_record(source, tags, description, logical_key, schema_ref, schema_version, content, learned_from="conversation") -> str`
- **Modified:** `update_entry` branches on `entry.type`:
  - `SCHEMA` → always `schema_immutable`.
  - `RECORD` with content change → re-resolve schema, re-validate, reject on failure.
  - `RECORD` attempting to change `schema_ref`/`schema_version` → `record_schema_pin_immutable`.
  - Other types → existing behavior.
- **Modified:** `delete_entry` branches on `entry.type == SCHEMA` to run deletion protection before soft-delete.

Response payloads trimmed to only what the caller didn't provide:

- `register_schema` returns `{"status": "ok", "id", "logical_key"}` (`logical_key` is server-derived).
- `create_record` returns `{"status": "ok", "id", "action": "created" | "updated"}`.

### EntryType additions (`src/mcp_awareness/schema.py`)

```python
class EntryType(str, Enum):
    # ... existing eight values ...
    SCHEMA = "schema"
    RECORD = "record"
```

No DB-level CHECK constraint on `entries.type` (there isn't one today); Python-layer `_parse_entry_type` handles invalid input with structured errors.

### CLI tool: `src/mcp_awareness/cli_register_schema.py`

New console script `mcp-awareness-register-schema`. Registered in `pyproject.toml` as `[project.scripts]`.

```
mcp-awareness-register-schema --system \
  --family schema:edge-manifest \
  --version 1.0.0 \
  --schema-file edge-manifest.json \
  --source awareness-built-in \
  --tags "schema,edge" \
  --description "Edge provider manifest schema"
```

Argparse validation, direct `PostgresStore` construction (no MCP / middleware / auth), writes with `owner_id="_system"` and `learned_from="cli-bootstrap"`. Skips embedding submission — CLI bootstrap shouldn't require an embedding provider.

## Data model

Both new types reuse the existing `Entry` dataclass and `entries` table. Schema body and record content live in the JSONB `data` column (**not** the `content` string field — avoids the Pydantic JSON-deserialization bug in awareness entry `5bc732c1`).

### Schema entry

```python
Entry(
    type=EntryType.SCHEMA,
    source=source,
    tags=tags,
    data={
        "family": "schema:edge-manifest",
        "version": "1.0.0",
        "schema": { ... JSON Schema body as dict ... },
        "description": description,
        "learned_from": learned_from,
    },
    logical_key="schema:edge-manifest:1.0.0",  # server-derived
    owner_id=current_owner(),                    # _system only via CLI
    language="english",
)
```

### Record entry

```python
Entry(
    type=EntryType.RECORD,
    source=source,
    tags=tags,
    data={
        "schema_ref": "schema:edge-manifest",
        "schema_version": "1.0.0",
        "content": { ... any JSON value, validated ... },
        "description": description,
        "learned_from": learned_from,
    },
    logical_key=caller_chosen,                   # supports upsert
    owner_id=current_owner(),                    # records never write to _system
    language=resolve_language(...),
)
```

### Uniqueness and lookup

The existing partial unique index `(owner_id, source, logical_key) WHERE logical_key IS NOT NULL AND deleted IS NULL` enforces:

- Per-(owner, source) uniqueness for both types via `logical_key`.
- Natural upsert path for records via the existing `remember`-style upsert machinery.

Cross-owner schema lookup issues a single query preferring caller-owned over `_system`:

```sql
SELECT * FROM entries
WHERE type = 'schema'
  AND logical_key = %(logical_key)s
  AND owner_id IN (%(caller)s, '_system')
  AND deleted IS NULL
ORDER BY CASE WHEN owner_id = %(caller)s THEN 0 ELSE 1 END
LIMIT 1
```

## `jsonschema` integration

- **Library version:** `jsonschema >= 4.26.0` (current PyPI latest, confirmed 2026-04-13). Added to main deps in `pyproject.toml` (not dev). Pulls `attrs`, `jsonschema-specifications`, `referencing`, `rpds-py` (wheels available for all supported platforms).
- **Meta-schema validation:** `Draft202012Validator.check_schema(schema_body)`. Raises `jsonschema.exceptions.SchemaError` on invalid schema.
- **Record validation:** `validator = Draft202012Validator(schema_body); errors = sorted(validator.iter_errors(content), key=lambda e: e.path)`.
- **Unknown keywords:** ignored by default (jsonschema v0.3+). Our future `x-secret` extension works "for free" without needing `validators.extend()` until we wire the secrets layer.
- **No validator caching** in v1 — construct per-write. Cache by `(owner_id, logical_key)` keyed on schema `id` if throughput demands later (schemas are immutable, so cache invalidation is trivial).
- **No `referencing.Registry`** in v1 — records reference schemas by our own `schema_ref`/`schema_version` pair, not JSON Schema `$ref`.
- **Belt-and-suspenders:** wrap both `check_schema()` and `iter_errors()` in try/except for `jsonschema.exceptions.JsonSchemaException` (base class); translate any unhandled exception to a generic `validation_error` structured response so raw tracebacks never reach agents.

## Data flow

### `register_schema` (MCP)

1. Tool handler receives `family, version, schema, source, tags, description, learned_from`.
2. `validation.validate_schema_body(schema)` → structured `invalid_schema` on failure.
3. Compose `logical_key = f"{family}:{version}"`.
4. Build `Entry(type=SCHEMA, ..., owner_id=current_owner())`.
5. `store.save_entry(entry)` → Postgres unique-constraint violation becomes `schema_already_exists`.
6. Submit to embedding pool (existing pattern).
7. Return `{"status": "ok", "id", "logical_key"}`.

### `create_record` (MCP)

1. Tool handler receives `logical_key, schema_ref, schema_version, content, source, tags, description, learned_from`.
2. `validation.resolve_schema(store, owner_id, schema_ref, schema_version)` → None if not found or soft-deleted.
   - None → `schema_not_found` structured error with `searched_owners: [caller, "_system"]`.
3. Extract `schema_body = resolved.data["schema"]`.
4. `validation.validate_record_content(schema_body, content)` → error list.
   - Non-empty → `validation_failed` with full list.
5. Build `Entry(type=RECORD, ...)` with caller-chosen `logical_key`.
6. `store.save_entry(entry)` — existing upsert path handles same-logical_key updates.
7. Return `{"status": "ok", "id", "action": "created" | "updated"}`.

### Record update (`update_entry`)

1. Load entry by ID; branch on `entry.type`.
2. `SCHEMA` → `schema_immutable`, always.
3. `RECORD`:
   - Update touches `content` → re-resolve schema, re-validate, reject on failure.
   - Update touches `schema_ref` or `schema_version` → `record_schema_pin_immutable`.
   - Update touches only non-content fields → no re-validation.
4. Write + append changelog per existing machinery.

### Schema delete (`delete_entry`)

1. Load entry; if `type == SCHEMA`, call `assert_schema_deletable`.
2. `count_records_referencing` → raise `schema_in_use` with blocker list if count > 0.
3. Soft-delete proceeds via existing machinery.

### CLI bootstrap

1. Argparse validates required args.
2. Read schema file as JSON.
3. `validation.validate_schema_body()` → stderr structured error + exit 1 on failure.
4. Build Entry with `owner_id="_system"`, `learned_from="cli-bootstrap"`, composed `logical_key`.
5. Construct `PostgresStore` directly (bypasses MCP, middleware, auth).
6. `save_entry()`. Skip embedding submission.
7. Print `{"status": "ok", "id", "logical_key"}` to stdout, exit 0.

## Error handling

All errors route through existing `_error_response()` helper (`helpers.py:214`) → structured `ToolError`. No new helper, no new envelope format.

### New error codes

| Code | Where | Retryable | Extra fields |
|---|---|---|---|
| `invalid_schema` | `register_schema` meta-schema failure | false | `schema_error_path`, `detail` |
| `invalid_parameter` | `register_schema` malformed `family`/`version` (existing code) | false | `param`, `value`, `valid` |
| `schema_already_exists` | `register_schema` unique-constraint collision | false | `logical_key`, `existing_id` |
| `schema_not_found` | `create_record` / record update | false | `schema_ref`, `schema_version`, `searched_owners` |
| `validation_failed` | record content fails schema | false | `schema_ref`, `schema_version`, `validation_errors`, `truncated?`, `total_errors?` |
| `schema_immutable` | `update_entry` on schema | false | — |
| `record_schema_pin_immutable` | record update tries to change pin fields | false | `param` |
| `schema_in_use` | `delete_entry` on referenced schema | false | `referencing_records`, `total_count?` |

### Validation error envelope shape

```json
{
  "error": {
    "code": "validation_failed",
    "retryable": false,
    "message": "Record content does not conform to schema edge-manifest:1.0.0 (2 errors)",
    "schema_ref": "schema:edge-manifest",
    "schema_version": "1.0.0",
    "validation_errors": [
      {
        "path": "/providers/0/name",
        "message": "'name' is a required property",
        "validator": "required",
        "schema_path": "/properties/providers/items/required"
      }
    ]
  }
}
```

- `path` from `ValidationError.json_path` — root is `/`, array indices included.
- `schema_path` is the JSON-Pointer-like path into the *schema* (`"/".join(str(p) for p in e.schema_path)`) — useful when the agent has the schema in hand for self-correction.
- `validator` is the failing JSON Schema keyword (`required`, `type`, `enum`, etc.) — enables keyword-specific remediation.
- List sorted by `path` for stable output.
- Truncated at 50 errors with `truncated: true, total_errors: <n>`.

## Deployment

### Alembic migration

`m8h9i0j1k2l3_add_system_user_for_schemas.py` (next sequential id; actual id assigned when authoring):

```sql
INSERT INTO users (id, display_name, created)
VALUES ('_system', 'System-managed schemas', now())
ON CONFLICT (id) DO NOTHING;
```

Single-purpose, idempotent, reversible. No DDL — leverages existing `users` table.

### Operator deploy sequence

1. Merge PR → Docker image rebuild on tag push (existing CI).
2. Pull + restart holodeck LXCs (production) **and** the QA instance (`docker-compose.qa.yaml`).
3. Run `mcp-awareness-migrate` in each environment — applies the `_system` user seed. **Not automatic; compose files do not run migrations at container start.** This matches the manual pattern used for all prior migrations (language/tsv backfills, OAuth columns, etc.).
4. Operator runs `mcp-awareness-register-schema --system ...` per built-in schema, gradually as schemas are authored. No requirement to seed all at deploy time.
5. No re-embed needed — existing entries unaffected.

### Compose files

All compose files (`docker-compose.yaml`, `docker-compose.qa.yaml`, `docker-compose.oauth.yaml`, `docker-compose.demo.yaml`) must remain coherent. **No changes required for this PR** — no new services, no new env vars, no new volumes, no new migration-at-start behavior.

### Rollback

`mcp-awareness-migrate --downgrade <prior-revision>` reverses the `_system` user seed. Any `schema`/`record` entries written during the deployment window remain in the DB as orphaned data on older code (unknown `EntryType` value → `_parse_entry_type` guard returns structured error). Re-rolling forward makes them visible again.

### Feature flag

None. The new tools are additive and opt-in. `_system` fallback only kicks in when a caller references a schema they don't own — opt-in by use.

## Testing strategy

### Unit tests: `tests/test_validation.py`

Pure functions, no DB. Covers:

- `validate_schema_body`: valid Draft 2020-12; invalid type value; non-object schema; empty `{}` (valid).
- `validate_record_content`: valid pass-through; multiple simultaneous failures; non-object content against non-object schema; `additionalProperties: false` behavior; truncation at 50.
- `compose_schema_logical_key`: format is `f"{family}:{version}"`.
- `resolve_schema` (with in-memory store stub): caller-owned present; `_system` fallback; caller wins over `_system`; soft-deleted excluded; neither exists.
- `assert_schema_deletable` (with store stub): passes with zero references; raises with blocker list.

### Integration tests: `tests/test_tools_schema_record.py`

Testcontainers Postgres. Covers:

- `register_schema`: happy path; duplicate; invalid meta-schema; malformed `family`/`version`.
- `create_record`: happy path; against `_system` schema; schema-not-found; validation failure; upsert via same `logical_key`.
- `update_entry` on record: valid content update; invalid content update (rejected); non-content update; attempt to change `schema_ref`/`schema_version` (rejected).
- `update_entry` on schema: any update rejected.
- `delete_entry` on schema: zero refs succeeds; with refs rejected with blocker list; after refs soft-deleted succeeds.
- `delete_entry` on record: unchanged behavior.
- Cross-owner isolation: A cannot see B's schemas; both see `_system`; A's records invisible to B.

### CLI tests: `tests/test_cli_register_schema.py`

- Happy path: valid file → entry with `owner_id="_system"`, stdout structured response.
- Invalid schema file: stderr structured error, exit 1, no entry written.
- Missing required args: argparse error, exit 2.
- `--source`, `--tags`, `--description` flow through to stored entry.
- `learned_from` hardcoded to `"cli-bootstrap"`.

### Existing tests to extend

- `tests/test_schema.py` — add `SCHEMA`/`RECORD` enum coverage.
- `tests/test_postgres_store.py` — add `find_schema` + `count_records_referencing` coverage.
- `tests/test_tools.py` — any parametrized entry-type tests include new values.

### Coverage discipline

- Per `feedback_codecov_coverage.md` and `feedback_local_coverage_before_qa.md`: run `pytest --cov` locally before marking Ready for QA.
- All new lines in `validation.py`, `cli_register_schema.py`, and the tool handlers covered. No `pragma: no cover` without explicit approval.

### Manual QA (PR body)

Per project convention — MCP-call steps on an alternate-port test instance. Exercises: register schema; write valid record; write invalid record (verify envelope shape); update record content (valid + invalid); attempt schema update (verify immutability); delete schema with records (verify protection); delete schema without records; `_system` fallback via CLI tool.

## PR conventions checklist

Per `CLAUDE.md`:

- [ ] CHANGELOG entry under `[Unreleased]`.
- [ ] README update if tool count or implemented-features sections change.
- [ ] Test count updated in README.
- [ ] `## QA` section in PR body with prerequisites + per-test checkboxes calling MCP tools.
- [ ] `QA Approved` label applied after manual QA.
- [ ] `docs/data-dictionary.md` updated with `schema`/`record` entry types and new `data` fields.
- [ ] Commit: AGPL v3 license preamble on every new `.py` file.

## Open questions for planning phase

None at design time. Items that will surface during planning:

- Exact naming of the next Alembic revision id (depends on head at implementation time).
- Whether to split the PR at the CLI tool boundary if the test suite grows unwieldy — design allows it but default is a single PR.
- Whether to add a short `docs/schema-record-guide.md` alongside the implementation for users (can be filed as follow-up).

## References

- Awareness design spec: `design-schema-record-secrets` (entry `53b378b2`, 2026-03-28)
- Active intention: `3117644f`
- Historical intention cancelled in this session: `42bb92e5` (superseded)
- GitHub issue: [#208](https://github.com/cmeans/mcp-awareness/issues/208)
- Downstream consumers: Layer A/B/C tag taxonomy design (`design-tag-taxonomy-v2`), awareness-edge runtime
- `jsonschema` Python library: `/python-jsonschema/jsonschema` (context7), docs on `check_schema`, `iter_errors`, `referencing.Registry`, custom keyword extension
- MCP Bench audit: entry `1373dbd5` — tool surface concerns driving the "no generic create_entry refactor in this PR" decision
- Existing structured-error helper: `src/mcp_awareness/helpers.py:214`
