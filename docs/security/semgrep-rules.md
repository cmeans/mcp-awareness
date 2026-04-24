<!-- SPDX-License-Identifier: AGPL-3.0-or-later | Copyright (C) 2026 Chris Means -->
# Semgrep rules — mcp-awareness

This document describes the project-specific Semgrep rules under `.semgrep/`.
Each rule exists to catch a regression class we've identified as likely or
costly. CI runs these alongside Semgrep's community rule packs
(`p/python`, `p/owasp-top-ten`) via `.github/workflows/semgrep.yml`; any
finding fails the build.

The general shape: Semgrep does the work; `.semgrep/` adds the
project-specific invariants the general packs can't know about.

## Rules

### `awareness-sql-missing-owner-id`

**File**: `.semgrep/awareness-sql-owner-scope.yml`

Every SQL statement that touches an owner-scoped awareness-data table
(`entries`, `reads`, `actions`, `embeddings`) must include `owner_id` in
its body. The `rls_store` + RLS policies defend at two layers at runtime;
this rule enforces the SQL-filter layer at authoring time so a regression
in either layer has a second defense.

**Why this rule exists.** The R1–R4 RLS harness (#359) was added precisely
because a tenant-isolation bug is project-ending at beta. The harness
catches runtime leaks; this rule catches the class of authoring mistake
that would cause the leak.

**Exceptions.** Templated SQL files that contain a `{placeholder}` (e.g.,
`{where}`, `{order_by}`) are auto-skipped — the Python caller in
`postgres_store.py` is responsible for injecting `owner_id` as a parameter,
and the RLS harness tests verify the result end-to-end. If you have a
deliberate cross-tenant statement (admin cleanup, shared-schema read),
add a comment containing the phrase `semgrep:allow
awareness-sql-missing-owner-id because <reason>` to the statement.

### `no-credential-identifier-in-logs`

**File**: `.semgrep/no-token-in-logs.yml`

Reject `logger.*` and `ctx.*` calls whose arguments interpolate variables
with credential-suggesting names. The match is case-insensitive, accepts
a single optional prefix (e.g., `oauth_access_token`, `bearer_token`,
`user_password`), and accepts a short tail of value-shape suffixes
(`_hash`, `_value`, `_b64`, `_bytes`, `_plain`). The base name set is:
`token`, `bearer`, `authorization`, `auth_header`, `password`, `passwd`,
`pwd`, `secret`, `client_secret`, `api_key`, `access_token`,
`refresh_token`, `id_token`, `credential`.

Matches: `token`, `Authorization`, `bearer_token`, `oauth_access_token`,
`password_hash`, `secret_bytes`. Non-matches (examples that would not
fire the rule): `token_bucket`, `api_key_label`,
`rate_limit_token_budget` — accept the gap; a misfire here is handled
by the "rename or suppress-with-justification" conversation, which is
preferable to the complexity of a taint-mode rule for a class of
mistake that's rare in practice.

The rule applies repo-wide, including `tests/`, because test code can
still accidentally log real fixture credentials into CI aggregators.
Tests that need a credential-shaped variable for a deliberate reason
(e.g., testing a redaction layer) should use the canonical per-line
suppression.

**Why this rule exists.** The empirical current state at rule authoring
(2026-04-23) had zero sites logging credentials — but preventive rules
have value precisely for classes of mistake that are easy to make and
hard to undo. An accidental `logger.info(f"got request {authorization}")`
on a busy log aggregator is a bad day; this rule catches it in CI.

**Not taint mode.** A taint-mode rule tracking auth-header → log would be
higher-fidelity but also higher-complexity; given zero current sites and
the simplicity of the variable-name heuristic, the pattern-based approach
is the right trade-off. If we accumulate evidence that the pattern is
routinely evaded via intermediate renames, upgrading to taint mode is a
clean follow-up.

**Suppress responsibly.** If a variable named (say) `token_label` is
genuinely not a credential, rename the variable. If suppression is the
right answer (e.g., a redaction-layer test that must construct a
credential-shaped value), use the canonical same-line form required by
`CONTRIBUTING.md`: `# nosemgrep: no-credential-identifier-in-logs
because <one-line reason>`. Bare `# nosemgrep` (without a rule ID)
would silently suppress every Semgrep finding on that line — including
unrelated future matches — and is disallowed by the project's
suppression policy.

### `no-sql-string-interpolation`

**File**: `.semgrep/no-sql-string-interpolation.yml`

Reject f-strings, `.format()`, and string concatenation as arguments to
`cursor.execute` / `conn.execute` / `executemany`. The project uses
parameterized queries throughout; this rule makes that a hard invariant
rather than a convention.

**Why this rule exists.** SQL injection via string interpolation is the
canonical SQL security bug. The project is not currently vulnerable
(audit at rule authoring found zero sites), but a single slip in a future
PR is easy to write and hard to spot in review. `cursor.execute(sql,
params)` is the correct form; `psycopg.sql.Composed`/`sql.Identifier` is
the correct form for dynamic identifiers.

**Suppress responsibly.** Genuine hard-coded SQL without runtime input
(e.g., admin one-shots building SQL from literal strings) can use
`# nosemgrep: no-sql-string-interpolation because <reason>` inline —
and the reason goes in the diff.

## Updating rules

- Rules live in `.semgrep/*.yml`; one rule per file keeps diffs readable.
- Add a local positive + negative test when changing a rule: stash a
  fixture under a throwaway path, run `semgrep --config .semgrep/
  /path/to/fixture`, verify the expected firings, remove the fixture.
- Avoid Semgrep-Pro-only features; CI runs in `--oss-only` mode.

## Running locally

```bash
pip install semgrep
semgrep --config .semgrep/ --error src/ tests/

# Same configuration CI uses (adds community rule packs):
semgrep \
  --config p/python \
  --config p/owasp-top-ten \
  --config .semgrep/ \
  --error --oss-only --metrics=off src/ tests/
```

## References

- Tracking issue: [#363](https://github.com/cmeans/mcp-awareness/issues/363)
- Semgrep docs: <https://semgrep.dev/docs>
- Community rule packs: `p/python`, `p/owasp-top-ten` (resolved by
  `semgrep ci` from the Semgrep registry)
