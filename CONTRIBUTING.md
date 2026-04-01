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

### How to sign

When you open your first pull request, you will be asked to sign the CLA.
This is a one-time requirement.

## Development setup

```bash
pip install -e ".[dev]"    # install with dev dependencies
python -m pytest tests/    # run tests (requires Docker for Postgres)
ruff check src/ tests/     # lint
ruff format src/ tests/    # format
mypy src/mcp_awareness/    # type check
```

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
