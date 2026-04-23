<!-- SPDX-License-Identifier: AGPL-3.0-or-later | Copyright (C) 2026 Chris Means -->
# Contributing to mcp-awareness

Thank you for your interest in contributing to mcp-awareness!

## Contributor License Agreement (CLA)

Before we can accept your contribution, you must agree to the Contributor
License Agreement. This is required because mcp-awareness uses dual licensing:

- **Open source**: AGPL-3.0-or-later (this repository)
- **Commercial**: A separate commercial license for organizations that cannot
  use AGPL-licensed software

The CLA grants the project maintainer (Chris Means) the right to sublicense
your contributions under any license, including proprietary. This preserves
the ability to offer both the open-source AGPL and commercial license options.
It does **not** transfer your copyright — you retain full ownership of your
contribution.

The full CLA text lives in [`CLA.md`](CLA.md).

### How to sign

Signing is automated via the [CLA Assistant](https://cla-assistant.io) bot.
The first time you open a pull request, the bot will:

1. Post a comment with a link to read the CLA and a sign-in prompt.
2. Set a pull request status check (`license/cla`) that blocks merge until
   you sign.

To sign, comment on the pull request with the **exact phrase** the bot
provides, which is:

> I have read the CLA Document and I hereby sign the CLA

The bot will re-check, mark the status green, and allow the PR to proceed.
Signing is a one-time action — the same signature applies to all your
future pull requests.

Your GitHub username and the timestamp of your signature are recorded in a
public Gist owned by the maintainer; see [`docs/cla.md`](docs/cla.md) for
the signature record location.

## Before opening a pull request

**File or link an issue first.** For anything beyond a typo fix or obvious
documentation correction, please open an issue describing what you want to
change and why, or link to an existing one. PRs without a linked issue may
be closed without review — not because your work isn't valued, but because
the discussion should happen somewhere visible before code is written.

If you're unsure whether something is worth an issue, open one anyway —
it's cheap, and a quick "yes, that's a real problem, go for it" saves you
time on code that might not fit the project's direction.

Issues stay permissive. Please don't let the PR expectation above discourage
you from reporting a bug or asking a question — that's what issues are for.

## Development setup

```bash
pip install -e ".[dev]"    # install with dev dependencies
pre-commit install         # install git hooks (runs gitleaks on commit)
python -m pytest tests/    # run tests (requires Docker for Postgres)
ruff check src/ tests/     # lint
ruff format src/ tests/    # format
mypy src/mcp_awareness/    # type check
```

`pre-commit install` is a one-time step per clone. Configured hooks live in
`.pre-commit-config.yaml`; the gitleaks hook blocks commits that contain
secrets. To bypass a known false positive, add its fingerprint to
`.gitleaksignore` with a comment explaining why; don't use `SKIP=gitleaks`
unless you know exactly what you're doing.

## Security scanners

CI runs several scanners in addition to the code checks:

- **gitleaks** (`.github/workflows/gitleaks.yml`) — scans commits for
  hardcoded secrets. See `.gitleaksignore` for the allowlist.
- **pip-audit** (`.github/workflows/pip-audit.yml`) — scans the installed
  Python dependency tree against the PyPA advisory database. Any known CVE
  in any installed package fails the build.
- **trivy** (runs as two steps inside `.github/workflows/docker-smoke.yml`)
  — scans the Dockerfile for misconfigurations and the built image for
  CVEs in the base layer + system packages. Only fires on PRs that touch
  the Dockerfile / pyproject.toml / uv.lock (matching the existing
  docker-smoke trigger set); non-image PRs don't rebuild the image and
  so don't re-scan.

### pip-audit failure policy

- CI runs `pip-audit --skip-editable` on a fresh venv containing this
  project plus its `[dev]` extras. Exit code 1 (any vulnerability) fails
  the build.
- `--skip-editable` excludes the project itself from the scan; it's
  installed in editable mode and isn't published to PyPI.
- Bootstrap tooling (`pip`, `setuptools`) is upgraded before the scan so
  venv-bootstrap CVEs don't count against the project — those packages
  aren't part of what the server ships.

When pip-audit fails a PR, the triage path is, in order of preference:

1. **Bump the dependency** to the fix version. Preferred — it's the
   upstream answer.
2. **Pin around the vuln** if the bump isn't available yet (e.g., wait
   for a compatible minor release). Record the pin as a TODO-pinned
   comment in `pyproject.toml` so it's visible.
3. **Accept + allowlist** via `--ignore-vuln CVE-ID` added to
   `pip-audit.yml` with a comment explaining why the exposure is
   acceptable (non-exploitable in this project's code path, upstream
   dispute, etc.). This is the escape hatch; every entry is reviewable
   in the workflow file itself.

Do not silently pin, downgrade, or suppress without documenting the
reasoning.

### trivy failure policy

- **Dockerfile config scan**: fails on `HIGH,CRITICAL` misconfigurations.
  Lower-severity findings (e.g., missing HEALTHCHECK, at LOW) appear in
  the run log but don't block the build. Address them when practical;
  don't hide them.
- **Image vulnerability scan**: fails on `HIGH,CRITICAL` CVEs that have
  an upstream fix available (`--ignore-unfixed`). A fixed CVE means
  "rebuild with a newer base image" and is actionable — bump the base,
  re-test, move on. Unfixed-upstream CVEs surface in the log but don't
  block CI because there's nothing to do about them until the fix is
  released; noisy failures on every PR would just train everyone to
  ignore the scanner.

When trivy fails, the triage path is the same shape as pip-audit:
1. Bump the base image / package (preferred).
2. Adjust the Dockerfile to avoid the misconfig.
3. Accept + allowlist via `.trivyignore` at repo root (one CVE ID or
   finding key per line, with an inline `#` comment explaining why).
   This is the escape hatch; every entry is visible in-repo.

## Pull request guidelines

- One concern per PR — don't mix unrelated changes
- Add changelog entries under `[Unreleased]` in `CHANGELOG.md`
- Update `README.md` if your change affects documented features or counts
- Update `docs/data-dictionary.md` if schema changed
- Every PR must include a `## QA` section with manual test steps
- CI runs pytest, ruff, and mypy automatically

## Code style

- Python 3.10+, strict mypy
- Ruff for linting and formatting (configured in `pyproject.toml`)
- 100-character line length

## AI-assistance disclosure

This project is developed with AI tools (Claude Code in particular).
Contributors using AI in their work are welcome — please disclose it on
the pull request so reviewers know what kind of review the code needs.
The PR template includes checkboxes for the common cases; "No AI used"
is a perfectly valid answer and so is "Copilot generated half of this."

What we'd rather not see: undisclosed AI work presented as hand-written,
especially when reviewers then spend time debugging patterns that would
have been obvious AI tells had they known. If in doubt, disclose.

## Do not commit secrets

Never include `.env` contents, credentials, API tokens, signing keys, or
production configuration values in a PR diff, commit message, or PR body.
If you're testing with real credentials, scrub them before committing.

mcp-awareness runs `gitleaks` as a pre-commit hook and as a CI gate
(`.github/workflows/gitleaks.yml`), but prevention beats detection —
please don't rely on the scanner to catch paste-ins for you. If you
accidentally push a secret, rotate it immediately; git history
rewriting is best-effort and public mirrors may already have the
value.
