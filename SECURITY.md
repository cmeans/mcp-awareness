<!-- SPDX-License-Identifier: AGPL-3.0-or-later | Copyright (C) 2026 Chris Means -->
# Security Policy

`mcp-awareness` is a single-maintainer project that stores potentially sensitive ambient data (status reports, knowledge, preferences). Taking reported vulnerabilities seriously matters more than how polished the paperwork looks, and this document commits the project only to processes that the maintainer can actually run today.

## Reporting a vulnerability

**Please do not report security vulnerabilities through public GitHub issues, discussions, or pull requests.** Public disclosure before a fix is available puts existing deployments at risk.

The preferred channel is **[GitHub's Private Vulnerability Reporting](https://github.com/cmeans/mcp-awareness/security/advisories/new)** on this repository (Security tab → **Report a vulnerability**). That opens a private advisory thread between you and the maintainer, creates a tracked case, and is the fastest route to an acknowledged receipt.

If Private Vulnerability Reporting is not available on the repository when you look — or you would rather not use GitHub — contact the maintainer directly via the email address listed on the [maintainer's GitHub profile](https://github.com/cmeans). A short "security issue in mcp-awareness — please contact me" is enough; do not include vulnerability details in the first message if you want an authenticated channel back before sharing details.

### What to include

When you're ready to share details, the following information speeds up triage and reduces round-trips:

- A description of the vulnerability and why it matters
- Repro steps (minimal, self-contained if possible)
- Affected versions, or the commit SHA you tested against
- Expected vs actual behavior
- Suggested remediation, if you have one
- Whether you'd like to be credited, and under what name/handle, if a fix ships

You do not need to produce a working exploit. A clear written description plus a repro path is enough to open a case.

## Response expectations

| Milestone | Target | What it means |
|-----------|--------|---------------|
| **Acknowledgement of receipt** | Within 72 hours | "Got it, looking at this" — not a triage verdict yet |
| **Triage outcome** | Within 14 days | Confirmed / not-reproducible / out-of-scope / duplicate, with reasoning |
| **Fix or mitigation** | Best effort, driven by severity | Critical/high issues take precedence over feature work; medium/low may be batched |
| **Coordinated public disclosure** | Up to 90 days from confirmation, or sooner once a fix is released | Published via GitHub Security Advisory on this repo |

If a report stalls past any of these windows without communication, please nudge the maintainer on the advisory thread — a solo-maintained project occasionally loses track of an open thread, and a nudge costs nothing.

## Scope

### In-scope for this policy

- **Server code in this repository** — the `mcp_awareness` Python package and anything under `src/`
- **Published container images** tagged from this repository (e.g., `:latest`, `:vX.Y.Z`)
- **Deployment artifacts** in this repository — `docker-compose.yaml`, Alembic migrations, workflow files, scripts checked into `scripts/`
- **Documentation** when it describes a security-relevant configuration (auth setup, deployment, data handling) and the documented configuration is unsafe

### Out-of-scope

- **Third-party dependencies.** Report upstream (e.g., directly to the psycopg, FastMCP, or pgvector maintainers). The maintainer will track upstream advisories and ship version bumps, but the initial report should go to whoever owns the code.
- **Hosted instances at `mcpawareness.com` or operated by the maintainer.** That deployment is maintainer-operated; unauthorized testing against it is not covered by this policy. If you believe a vulnerability in the code affects the hosted instance, report it per the process above and the maintainer will coordinate any testing needed to confirm.
- **Edge collector repositories** (e.g., `homelab-edge` and similar sibling repos). Each of those repositories has its own `SECURITY.md` and separate maintenance.
- **Bugs unrelated to security** — functional bugs, feature requests, documentation typos, UX papercuts. File those as normal public issues.
- **Social-engineering or physical access** attacks, and **denial-of-service through brute resource consumption** against the hosted instance.

## Safe harbor

Provided your research and disclosure comply with this policy — in particular, you don't access or modify data that isn't yours, you don't disrupt production services beyond the minimum needed to demonstrate a flaw, and you give the maintainer reasonable time to respond before public disclosure — the maintainer commits to:

1. **Not pursue civil legal action** against you for your research
2. **Not refer you for criminal investigation** for your research
3. **Work with you in good faith** to understand and resolve the reported issue

This safe harbor covers good-faith security research. It does not cover data exfiltration beyond what's needed to demonstrate a vulnerability, secondary sale or publication of user data, or any activity that would violate applicable law independent of the research context.

## Credit

When a reported vulnerability results in a fix:

- The reporter is credited in the published GitHub Security Advisory on this repository (unless you prefer to stay anonymous — say so in the report).
- The fix commit and release notes mention the reporter by the name/handle they specify.

There is **no standalone Hall of Fame page, no paid bug bounty, and no formal CVE assignment program.** If a CVE is appropriate, the maintainer will work with GitHub's CNA process to request one through the published advisory, but this is not a guarantee and the request process takes as long as GitHub takes.

## What's not covered

- **Bug bounty payments.** There is no bounty program. The maintainer does not pay for security reports, and no issue, PR, or comment in this repository authorizes a bounty regardless of its framing.
- **Pre-fix credit or publicity.** The maintainer will not issue public statements about an unresolved vulnerability beyond what's in the (private) advisory thread.
- **SLA-like commitments beyond the response-expectation table above.** The targets in that table are best-effort for a single-maintainer project in pre-beta; the maintainer will communicate openly when a target slips.

## Changes to this policy

This document is versioned in git along with the rest of the repository. Non-trivial changes (scope, response targets, disclosure windows) are announced under `CHANGELOG.md` and called out in the corresponding release notes.
