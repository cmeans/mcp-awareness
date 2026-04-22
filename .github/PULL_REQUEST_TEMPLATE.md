<!--
Thanks for contributing to mcp-awareness! Please fill out each section. If a
section doesn't apply, write "N/A" with a short reason — please don't delete
the headings, they help reviewers find information predictably.

If this is your first PR, you'll also need to sign the CLA via the
`cla-assistant` bot comment. That takes ~30 seconds and applies to all
future contributions.
-->

## Linked issue

Fixes # <!-- issue number, or "none" with justification -->

<!--
For anything beyond a typo fix or obvious doc correction, please open an issue
first and link it here. PRs without a linked issue may be closed without review
so discussion happens in the issue thread. If unsure, open an issue anyway —
it's cheap, and a quick "yes, go for it" saves work on something that might not
fit the project's direction. See CONTRIBUTING.md §"Before opening a pull request".
-->

## Summary

<!-- What does this PR change, and why? 1-3 sentences is usually enough. -->

## Scope

<!--
Paste `git diff --stat origin/main` here so reviewers can confirm there's no
unintended scope creep.
-->

## AI-assistance disclosure

<!--
mcp-awareness is AI-friendly — this is disclosure, not prohibition. Check all
that apply, including "No AI used" if that's accurate. See CONTRIBUTING.md
§"AI-assistance disclosure".
-->

- [ ] No AI used in producing this PR
- [ ] AI assisted with code generation (e.g., Copilot, Cursor, Claude Code)
- [ ] AI assisted with review / suggestions during authoring
- [ ] AI assisted with the PR body or commit messages

## QA

### Prerequisites

<!--
Environment setup required to exercise the change (deps to install, alternate
port like `AWARENESS_PORT=8421` to avoid breaking a running instance, test
data to load, etc.). If none, write "None".
-->

### Manual tests (via MCP tools)

<!--
Testing goes through the MCP tool surface — not raw HTTP. Each step should
name the MCP tool + arguments, and describe the expected outcome.
-->

1. - [ ] **Test description**
   ```
   tool_name(arg1="value", arg2="value")
   ```
   Expected: description of what success looks like

## Checklist

- [ ] `CHANGELOG.md` entry added under `[Unreleased]` in Keep-a-Changelog format
- [ ] `README.md` and `docs/data-dictionary.md` updated if affected (tool count, schema, documented features)
- [ ] No secrets, credentials, API tokens, signing keys, or `.env` contents included in the diff
- [ ] `ruff check`, `mypy`, and `pytest` pass locally
- [ ] I have read and will sign the [CLA](../CLA.md) via the `cla-assistant` bot
