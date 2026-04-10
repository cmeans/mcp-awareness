<!-- SPDX-License-Identifier: AGPL-3.0-or-later | Copyright (C) 2026 Chris Means -->
# Trim Write-Tool Response Echoes Design

> **GitHub Issue:** #243
> **Status:** Design awaiting QA review (no code yet)
> **Date:** 2026-04-10
> **Sequencing:** Pre-cursor before Layer 1 implementation (#238). Layer 1 will
> heavily edit `tools.py`; landing this trim first keeps the Layer 1 diff clean.

## Problem

Write-tool responses echo back input fields the caller already has in context.
For every `remember`, `learn_pattern`, `add_context`, `update_entry` call, the
response includes the full `description` and sometimes more. The caller just
sent that data. Pure token waste.

This violates the standing token-efficiency directive — *minimize client-side
token consumption, push work to the server* — at the smallest possible scope
(the response shape of a single tool call).

## Source

Feedback from Claude Desktop (awareness entry
`2e2ff1a0-8dc2-452e-be4a-68aeea906f94`, logical_key `issue-trim-write-responses`,
2026-04-10):

> Reduce token waste in write-tool responses: `remember`, `learn_pattern`,
> `add_context`, `update_entry` all echo the full description back in their
> response. The caller already has it in context. Drop description from
> responses, returning only `{status, id, action}`. Audit all write tools
> in one pass.

CD's original feedback was narrower (4 tools). This design extends the audit
to all 13 write tools but applies a pragmatic rule that keeps self-describing
handles in place.

## Goal & non-goals

**Goal.** Eliminate redundant input echoes from MCP write-tool responses while
keeping responses self-describing (logs, tests, and primary handles still
parse cleanly). Add a regression-proofing test that grows with future write
tools.

**Non-goals.**
- Read tools (`get_*`, `semantic_search`, etc.) — they legitimately return
  content the caller did not send. Out of scope.
- Error responses — still need structured error payloads with enough context
  for the caller to self-correct. Out of scope.
- Tool semantics, behavior, or argument shapes — pure response-shape cleanup.
- Schema or migration changes.
- README or data-dictionary updates (no schema or feature claims affected).

## Design rule (interpretation B — pragmatic)

A field MAY appear in a write-tool response if and only if at least one of:

1. It is **server-generated** (e.g., new entry `id`, action record `id`,
   `timestamp`, `updated`, `expires` computed from `now + duration`).
2. It is **server-aggregated** (e.g., `restored` count, `trashed` count).
3. It is **server-derived initial state** (e.g., `state: "pending"` for
   `remind`).
4. It is a **primary handle** for the operation — an identifier the caller
   passed in that is used as a lookup key, upsert key, or operation target,
   AND including it keeps logs and tests self-describing. Examples:
   `alert_id` (upsert key for `report_alert`), `key`+`scope` (compound upsert
   key for `set_preference`), `entry_id` (operation target for
   `restore_entry`/`update_intention`/`acted_on`).

A field MUST NOT appear in a write-tool response if it is **caller-supplied
payload data** with no handle role:
- Free-text descriptions, content, messages, reasons
- Metadata fields like `platform`, `detail`, `tags` when they are not the
  primary lookup mechanism
- The `value` of a key/value preference

The rule was chosen over strict "no echoes at all" because handles are
load-bearing for self-describing logs and tests — and the user explicitly
preferred B during brainstorming.

### Special case: `delete_entry` single-entry mode

`delete_entry` with `entry_id` returns
`{status: "acknowledged", entry_id, recoverable_days, note}` *uniformly*
regardless of whether the entry existed. This is the IDOR-safety contract
established by #193 / PR #234. The `entry_id` echo is not waste — it is part
of a security invariant ("response shape MUST NOT distinguish hit from miss").
This design does not touch it.

## Audit and changes

### Tools that change (6)

| Tool | Before | After | Why |
|---|---|---|---|
| `report_status` | `{status, id, source}` | `{status, id, action: "reported"}` | `source` is caller payload, not a handle; `action` makes the response symmetric with create-style tools |
| `learn_pattern` | `{status, id, description}` | `{status, id, action: "created"}` | `description` is payload |
| `remember` (no key) | `{status, id, description}` | `{status, id, action: "created"}` | `description` is payload |
| `remember` (logical_key) | `{status, id, action, description}` | `{status, id, action}` | drop redundant `description`; `action` already distinguishes `created` vs `updated` |
| `set_preference` | `{status, key, value, scope}` | `{status, id, action: "set", key, scope}` | drop `value` (payload); add `id` from store; keep `key`+`scope` (compound handle) |
| `acted_on` | `{status, id, entry_id, timestamp, platform, action, detail, tags}` | `{status, id, entry_id, action, timestamp}` | drop `platform`/`detail`/`tags` echoes from store dict; keep `entry_id` (handle) and `action` (primary effect label) |
| `update_intention` | `{status, id, state, reason}` | `{status, id, state}` | drop `reason` (free-text echo); keep `state` (the effect of the operation) |

### Tools that do NOT change (7)

| Tool | Current shape | Why unchanged |
|---|---|---|
| `report_alert` | `{status, id, action, alert_id}` | `alert_id` is the upsert handle |
| `update_entry` | `{status, id, updated}` | already minimal |
| `suppress_alert` | `{status, id, expires}` | `expires` is server-computed |
| `add_context` | `{status, id, expires}` | `expires` is server-computed |
| `delete_entry` (single) | `{status: "acknowledged", entry_id, recoverable_days, note}` | IDOR contract from #234 — uniform response invariant |
| `delete_entry` (bulk + dry_run) | `{status, trashed/would_trash, source/tags/entry_type, recoverable_days, message}` | Operator confirmation UX — echoes are needed so the operator can verify what was matched before/after `confirm=True` |
| `restore_entry` | `{status, restored, entry_id}` or `{status, restored, tags}` | `entry_id`/`tags` are handles |
| `remind` | `{status, id, state: "pending"}` | `state` is server-set initial state, not echoed input |

### Behavioral compatibility note

These are **response-shape changes only**. Request shapes are unchanged. Any
client that read echoed payload fields from a write response will break. The
fix is trivial (the caller already has what it sent), but the CHANGELOG must
spell out the per-tool deltas so consumers can audit.

Audit of repo for write-response field reads after `json.loads`: no internal
consumer reads dropped fields. The two known external consumers (Claude Code
in this session and Claude Desktop, which is the source of the feedback) do
not depend on the echoes.

## Implementation strategy — Approach (i) Surgical

Chosen over (ii) "surgical + helpers" and (iii) "schema-driven response models"
because the issue is explicitly scoped as a trim, not a refactor; the
sentinel-scan test (below) is the real quality gate; helpers and types add
review surface for no proportional benefit at this size; and Layer 1 work is
queued behind this PR.

**Files touched (3):**
- `src/mcp_awareness/tools.py` — 6 function bodies + 6 docstrings.
- `tests/test_server.py` — update existing assertions on dropped fields,
  add new `TestWriteResponseShapes` class.
- `CHANGELOG.md` — entry under `[Unreleased]` with per-tool sub-bullets.

No new modules, no new dependencies, no schema changes, no migration.

## Test plan

### New test class — `TestWriteResponseShapes`

A single new class in `tests/test_server.py` with three test functions plus
two registry entries.

**Mechanic.** Call each write tool with kwargs whose string values have been
wrapped in recognizable sentinels (e.g.,
`f"SENTINEL_{key}_{uuid.uuid4().hex[:8]}"`). Parse the JSON response. Walk it
recursively. Any sentinel string found at a path that does not terminate at
an exempt key is a leak.

**Per-tool exemption registry** — the executable spec for what counts as a
handle vs payload:

```python
ECHO_EXEMPTIONS: dict[str, set[str]] = {
    "report_status":   set(),
    "report_alert":    {"alert_id"},
    "learn_pattern":   set(),
    "remember":        set(),
    "update_entry":    set(),
    "suppress_alert":  set(),
    "add_context":     set(),
    "set_preference":  {"key", "scope"},
    "delete_entry":    {"entry_id", "tags", "source", "entry_type"},
    "restore_entry":   {"entry_id", "tags"},
    "acted_on":        {"entry_id", "action"},
    "remind":          set(),
    "update_intention":{"id", "state"},
}
```

The registry doubles as documentation of the rule. Future write tools must
add themselves to it; if they don't, the registry-completeness test below
fails loudly.

**Three tests:**

1. **`test_no_caller_input_echoed`** (parametrized over all write tools).
   Calls each write tool with sentinel inputs, asserts no non-exempt sentinel
   value appears anywhere in the response.

2. **`test_write_tool_registry_complete`.** Introspects `_srv.mcp` to find
   every registered write tool, asserts every write tool is present in
   `ECHO_EXEMPTIONS`. Catches "added a new tool but forgot the exemption
   entry".

3. **`test_exemption_registry_no_stale_entries`.** Asserts every key in
   `ECHO_EXEMPTIONS` corresponds to a real registered tool. Catches stale
   entries when tools are renamed or removed.

**Identifying "write tools" for the registry-complete test.** Awareness has
no formal `write_tool` decorator. We use a hand-curated list inside the test
file (the keys of `ECHO_EXEMPTIONS`), and the registry-complete test
cross-checks it against `_srv.mcp` plus an *expected non-write* exclusion set
(read tools, lifecycle tools). This is a small maintenance cost — adding a
new write tool requires adding it to the registry — but the alternative
(magic introspection) would be more brittle and harder to read.

### Existing test updates (mechanical)

Removals only — no new behavioral assertions in existing tests:
- `tests/test_server.py::TestReportStatusTool` — remove
  `assert data["source"] == "nas"` assertions
- `tests/test_server.py::TestLearnPatternTool` — remove
  `assert data["description"] == ...` assertions
- `tests/test_server.py::TestRememberTool` — remove `description` echo
  assertions (none assert it directly today, but the tool's logical-key
  branch test will need a touch)
- `tests/test_server.py::TestSetPreferenceTool` — remove
  `assert data["key"]/["value"]/["scope"] == ...` and add an assertion
  that `id` is now present
- `tests/test_server.py::TestActedOnTool` — remove
  `assert result["tags"] == ...` assertion (still verify the action was
  recorded, but read it back via `get_actions` instead of from the tool
  response)
- `tests/test_server.py::TestIntentionTools` — remove
  `assert fired["state"] == "fired"` echo assertion only if the new shape
  drops it (it does NOT — `state` is exempt as the primary effect, so
  this test stays)

Estimated edits: ~12 lines removed, ~3 lines added across existing tests.

### Test count delta

- Before: 764
- New class: 3 functions, of which 1 is parametrized over 13 write tools
- Pytest counting: parametrized cases count individually
- Expected after: 764 + 13 (parametrized) + 2 (registry-complete +
  no-stale) = 779
- Update README test count claim only if it has a number (verify before
  push — current README uses "Comprehensive test suite", no number, so
  no update needed)

## Docstring updates

Each of the 6 changed tools gets a `Returns:` block appended to its docstring
documenting the exact response shape, e.g.:

```python
async def remember(...) -> str:
    """Store permanent knowledge — facts that will still be true in 30 days.
    ...

    Returns:
        JSON: {"status": "ok", "id": "<uuid>", "action": "created" | "updated"}
        action="updated" is only returned when logical_key matched an
        existing entry.
    """
```

Untouched tools get no docstring change (out of scope to avoid noise in this
diff).

## CHANGELOG

Add to `[Unreleased]` under `### Changed`:

```markdown
### Changed
- **perf:** trim echoed input from write-tool responses to reduce token
  waste (#243):
  - `report_status` no longer echoes `source`; now returns
    `{status, id, action: "reported"}`
  - `learn_pattern` no longer echoes `description`; now returns
    `{status, id, action: "created"}`
  - `remember` no longer echoes `description`; now returns
    `{status, id, action}` (`created`|`updated`)
  - `set_preference` no longer echoes `value`; now returns
    `{status, id, action: "set", key, scope}` (now includes `id`)
  - `acted_on` no longer echoes `platform`/`detail`/`tags`; now returns
    `{status, id, entry_id, action, timestamp}`
  - `update_intention` no longer echoes `reason`; now returns
    `{status, id, state}`
  - **Breaking for clients that read echoed input fields from these write
    responses.** Other write tools are unchanged (handles or server-derived
    fields preserved).
```

## Risk & rollout

- **Blast radius.** External clients that read echoed payload fields from a
  write response will break. The break is trivial to fix (caller already has
  the data it sent). Known external consumers (Claude Code, Claude Desktop)
  do not read echoed fields.
- **Internal blast radius.** Repo-wide search for write-response field reads
  after `json.loads`: none found in the audit pass.
- **Rollback.** Single commit, trivially revertable. No schema or migration
  changes.
- **Sequencing.** Lands on `main` before Layer 1 (#238) is pushed so
  `feat/hybrid-retrieval-layer1` can rebase onto a clean main without thread
  unrelated response-shape changes through its diff.
- **Release.** Does not trigger a release on its own. Will roll into the
  next release alongside Layer 1 work.

## Out-of-scope (explicit "no" list)

- Read tools — out of scope per issue.
- Error response shapes — out of scope per issue.
- Helper extraction (declined approach (ii) during brainstorming).
- Pydantic / TypedDict response schemas (declined approach (iii)).
- Touching `delete_entry` IDOR contract from #234.
- README / data-dictionary updates — no schema or test-count claim is
  materially affected.
- New write tools — out of scope; this PR only shapes existing ones.
- Renaming or rebadging tools.

## Acceptance criteria

- [ ] Each of the 6 changed tools returns the exact shape documented above
- [ ] Each of the 7 unchanged tools returns its current shape (regression
      check via existing tests)
- [ ] `TestWriteResponseShapes` class with 3 test functions exists
- [ ] `ECHO_EXEMPTIONS` registry contains all 13 write tools
- [ ] `test_write_tool_registry_complete` passes (registry covers all
      registered write tools)
- [ ] `test_exemption_registry_no_stale_entries` passes
- [ ] Docstrings of the 6 changed tools have a `Returns:` block
- [ ] CHANGELOG entry under `[Unreleased]` lists per-tool deltas with
      breaking-change call-out
- [ ] CI green (ruff, mypy, pytest)
- [ ] Manual MCP smoke test (in QA section of PR) confirms each changed tool
      returns the new shape via the MCP client interface

## References

- GitHub issue: cmeans/mcp-awareness#243
- Source feedback: awareness entry `2e2ff1a0-8dc2-452e-be4a-68aeea906f94`
  (logical_key `issue-trim-write-responses`, learned_from `claude.ai`)
- Standing directive: `feedback_token_efficiency.md` — minimize client-side
  token consumption, push work to the server
- Related: PR #234 / issue #193 — `delete_entry` IDOR fix established the
  uniform-single-delete-response contract this design respects
- Related: issue #238 — Layer 1 hybrid retrieval, the feature work this PR
  pre-cursors
