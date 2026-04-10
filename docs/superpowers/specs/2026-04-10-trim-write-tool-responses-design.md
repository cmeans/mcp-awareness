<!-- SPDX-License-Identifier: AGPL-3.0-or-later | Copyright (C) 2026 Chris Means -->
# Trim Write-Tool Response Echoes Design

> **GitHub Issue:** #243
> **Design PR:** #244
> **Status:** Round-2 — QA round-1 substantive items resolved, awaiting QA review
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

### Sub-rule: `action` only when dynamic

> **Added in round-2 after QA Substantive 2.** A static `action` string carries
> zero information — `report_status` returning `action: "reported"` is implied
> by *the name of the tool that was just called*. On a token-efficiency PR,
> paying ~14 tokens per call for a constant string is self-defeating.
>
> Therefore: **`action` is included in a write-tool response only when its
> value is dynamically determined** (the tool branches between ≥2 outcomes).
> This narrows `action`-bearing responses to: `remember` (logical_key path
> distinguishes `created` vs `updated`), `report_alert` (`reported` vs
> `resolved`), and `acted_on` (caller-supplied effect label).

### Tools that change (5)

| Tool | Before | After | Why |
|---|---|---|---|
| `learn_pattern` | `{status, id, description}` | `{status, id}` | `description` is payload; `learn_pattern` always creates (no upsert path), so `action` would be a constant — drop |
| `remember` (no key) | `{status, id, description}` | `{status, id}` | `description` is payload; no upsert path on this branch, so no `action` |
| `remember` (logical_key) | `{status, id, action, description}` | `{status, id, action}` | drop redundant `description`; presence of `action` itself signals the upsert path was taken (`created` vs `updated`) |
| `set_preference` | `{status, key, value, scope}` | `{status, id, key, scope}` | drop `value` (payload); add `id` from store (symmetric with create-style tools); keep `key`+`scope` (compound upsert key — same handle role as `source` in `report_status`); `action` would be a constant `"set"` — drop |
| `acted_on` | `{status, id, entry_id, timestamp, platform, action, detail, tags}` | `{status, id, entry_id, action, timestamp}` | drop `platform`/`detail`/`tags` echoes from store dict; keep `entry_id` (handle) and `action` (caller-supplied effect label — the substance of the action record, not a payload echo) |
| `update_intention` | `{status, id, state, reason}` | `{status, id}` | drop `reason` (free-text echo); drop `state` — verified in code (`tools.py:975-998`) to be a pure pass-through with no coercion or auto-advancement, so it is echoed input under the rule |

### Tools that do NOT change (8)

| Tool | Current shape | Why unchanged |
|---|---|---|
| `report_status` | `{status, id, source}` | `source` is the upsert key (verified: `upsert_status(owner_id, source, ...)`) — same handle role as `key`+`scope` in `set_preference` and `alert_id` in `report_alert`. Already minimal. |
| `report_alert` | `{status, id, action, alert_id}` | `alert_id` is the upsert handle; `action` is dynamic (`reported` vs `resolved`) |
| `update_entry` | `{status, id, updated}` | already minimal |
| `suppress_alert` | `{status, id, expires}` | `expires` is server-computed |
| `add_context` | `{status, id, expires}` | `expires` is server-computed |
| `delete_entry` | single mode: `{status: "acknowledged", entry_id, recoverable_days, note}` (uniform regardless of hit/miss); bulk + dry_run modes: `{status, trashed/would_trash, source/tags/entry_type, recoverable_days, message}` | Single mode: IDOR contract from #234, uniform-response invariant. Bulk modes: operator confirmation UX — echoes are needed so the operator can verify what was matched before/after `confirm=True`. |
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
- `src/mcp_awareness/tools.py` — 5 function bodies + 5 docstrings.
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
    "report_status":    {"source"},                   # upsert key
    "report_alert":     {"alert_id"},                 # upsert key
    "learn_pattern":    set(),
    "remember":         set(),                        # `action` (when present) is server-derived, not exempt-listed
    "update_entry":     set(),
    "suppress_alert":   set(),
    "add_context":      set(),
    "set_preference":   {"key", "scope"},             # compound upsert key
    "delete_entry":     {"entry_id", "tags",
                          "source", "entry_type"},     # IDOR contract + bulk confirmation UX
    "restore_entry":    {"entry_id", "tags"},         # handles
    "acted_on":         {"entry_id", "action"},       # handle + caller-supplied effect label
    "remind":           set(),
    "update_intention": {"id"},                       # "id" = caller-supplied entry_id (lookup target),
                                                       # NOT a server-generated entry id
}
```

> **Note on `remember`'s empty exemption set.** `remember` (logical_key path)
> returns `action: "created" | "updated"`, but `action` is *server-derived*
> (the store decides which branch ran), not echoed input. The exemption
> registry only lists caller-supplied fields that are allowed to round-trip,
> so server-derived fields don't appear there. The sentinel scan walks the
> response looking for caller-sentinel strings; since `"created"` /
> `"updated"` are hardcoded server-side, no sentinel matches them and no
> exemption is needed.

> **Note on the sentinel scan's string-only matching.** The sentinel approach
> wraps caller-supplied *string* values with recognizable markers. If a
> future write tool ever echoes a numeric or boolean payload field, the
> sentinel scan won't catch it. The current 13-tool surface only echoes
> strings, and the few numeric inputs (TTLs, counts) are not echoed back.
> The implementation should add a comment at the top of
> `TestWriteResponseShapes` calling out this limitation so a future
> contributor adding an echoed numeric field doesn't assume the test will
> catch it.

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

- `tests/test_server.py::TestReportStatusTool` — **no changes.**
  `report_status` is now in the unchanged set; existing
  `assert data["source"] == "nas"` assertion stays valid.
- `tests/test_server.py::TestLearnPatternTool` — remove
  `assert data["description"] == ...` assertions; the new shape is
  `{status, id}` so only `status` and `id` can be asserted.
- `tests/test_server.py::TestRememberTool` — remove `description` echo
  assertions; verify the no-key branch returns `{status, id}` (no
  `action`) and the logical_key branch returns `{status, id, action}`
  (with `action` ∈ {`created`, `updated`}).
- `tests/test_server.py::TestSetPreferenceTool` — remove
  `assert data["key"]/["value"]/["scope"] == ...` (the `key`/`scope`
  assertions can stay since both are exempt handles, but `value` must
  go); add an assertion that `id` is now present.
- `tests/test_server.py::TestActedOnTool` — remove
  `assert result["tags"] == ...` assertion (still verify the action was
  recorded, but read it back via `get_actions` instead of from the tool
  response).
- `tests/test_server.py::TestIntentionTools::test_update_intention_state`
  — remove `assert fired["state"] == "fired"` and
  `assert completed["state"] == "completed"`. The new
  `update_intention` shape is `{status, id}`; verify the state
  transition by reading the intention back via `get_intentions` instead
  of asserting on the response.

Estimated edits: ~14 lines removed, ~5 lines added across existing tests.

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

Each of the 5 changed tools gets a `Returns:` block appended to its docstring
documenting the exact response shape, e.g.:

```python
async def remember(...) -> str:
    """Store permanent knowledge — facts that will still be true in 30 days.
    ...

    Returns:
        JSON: {"status": "ok", "id": "<uuid>"} for normal calls.
        When logical_key is provided, additionally includes
        "action": "created" | "updated" — presence of the field signals
        the upsert path was taken; "updated" means logical_key matched
        an existing entry.
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
  - `learn_pattern` no longer echoes `description`; now returns
    `{status, id}`
  - `remember` no longer echoes `description`; now returns `{status, id}`
    on the normal path, or `{status, id, action}` (`created`|`updated`)
    when `logical_key` is provided
  - `set_preference` no longer echoes `value`; now returns
    `{status, id, key, scope}` (now includes `id`; `key`+`scope` retained
    as the compound upsert handle)
  - `acted_on` no longer echoes `platform`/`detail`/`tags`; now returns
    `{status, id, entry_id, action, timestamp}`
  - `update_intention` no longer echoes `state` or `reason`; now returns
    `{status, id}`
  - **Breaking for clients that read echoed input fields from these write
    responses.** Other 8 write tools (`report_status`, `report_alert`,
    `update_entry`, `suppress_alert`, `add_context`, `delete_entry`,
    `restore_entry`, `remind`) are unchanged — handles or server-derived
    fields only.
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

- [ ] Each of the 5 changed tools returns the exact shape documented above
- [ ] Each of the 8 unchanged tools returns its current shape (regression
      check via existing tests)
- [ ] No write tool returns a static `action` string (the sub-rule); only
      `remember` (logical_key path), `report_alert`, and `acted_on` carry
      `action`, all dynamically determined
- [ ] `TestWriteResponseShapes` class with 3 test functions exists
- [ ] `ECHO_EXEMPTIONS` registry contains all 13 write tools
- [ ] `test_write_tool_registry_complete` passes (registry covers all
      registered write tools)
- [ ] `test_exemption_registry_no_stale_entries` passes
- [ ] Docstrings of the 5 changed tools have a `Returns:` block
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

## Revision history

### Round 1 → Round 2 (QA round-1 findings, resolved 2026-04-10)

QA review of round-1 (PR #244) flagged three substantive consistency
issues. All resolved in this revision:

1. **`report_status` vs `set_preference` handle inconsistency.** Round-1
   draft kept `key`+`scope` for `set_preference` (compound upsert key) but
   dropped `source` for `report_status` despite `source` being functionally
   identical (the upsert key for `upsert_status`). Fixed by keeping
   `source` in `report_status` — it now joins the unchanged-tools list.
   Rule: a caller-supplied identifier used as an upsert key is a handle.

2. **Static `action` strings carry zero information.** Round-1 draft added
   `action: "reported"` to `report_status`, `action: "created"` to
   `learn_pattern`, and `action: "set"` to `set_preference`. All three
   are constants with no informational content — paying ~14 tokens per
   call to confirm a fact already implied by the tool name. Fixed by
   adding the **"`action` only when dynamic"** sub-rule: `action` is
   included in a write-tool response only when its value is dynamically
   determined. Narrows `action`-bearing responses to `remember`
   (logical_key path), `report_alert`, and `acted_on`. The presence of
   `action` in `remember`'s response is itself information — clients
   know the upsert path was taken iff `action` is present.

3. **`update_intention.state` is verified-pure echo.** Round-1 draft kept
   `state` in the response with the rationale "state IS the effect of
   the operation." Code inspection (`tools.py:975-998`) showed `state`
   is validated then passed through unchanged with no coercion or
   auto-advancement, making it textbook echoed input. Fixed by dropping
   `state` from the response. Final shape is `{status, id}`. If a
   future change makes `update_intention` actually transform `state`,
   re-add it with a comment explaining why it's exempt.

QA round-1 also flagged four observations, all addressed in this
revision: (a) disambiguation comment on `update_intention`'s exemption
registry entry (`id` means caller-supplied entry_id, not server-generated);
(b) note on the sentinel scan's string-only matching limitation;
(c) merged `delete_entry`'s two-mode rows in the unchanged table;
(d) precision on `learn_pattern`'s rationale (no upsert path, so any
`action` would be constant).

**Tally before round-2:** 6 changed + 7 unchanged = 13.
**Tally after round-2:** 5 changed + 8 unchanged = 13. `report_status`
moved from changed to unchanged.
