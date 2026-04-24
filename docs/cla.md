<!-- SPDX-License-Identifier: AGPL-3.0-or-later | Copyright (C) 2026 Chris Means -->
# CLA enforcement

Every external contribution to mcp-awareness is gated on a signed
[Contributor License Agreement](../CLA.md). This document describes how the
gate works in practice and where the signature record lives.

## Bot

CLA enforcement is handled by [CLA Assistant](https://cla-assistant.io), a
hosted GitHub App maintained by SAP. It installs a webhook on the repository,
posts a sign-in comment on each new pull request from an unsigned author, and
sets a `license/cla` status check that blocks merge until signed.

## CLA text

Authoritative text: [`CLA.md`](../CLA.md) in the repository root.

The CLA Assistant bot reads the same content from a public Gist that mirrors
`CLA.md`. If the CLA text changes (a version bump), the Gist must be updated
in lockstep so contributors and the bot see the same agreement.

## Signature record

Signatures are stored as a public GitHub Gist owned by the maintainer:

**https://gist.github.com/cmeans/cb1b0c5535b68011af8fd0abd0e46850**

Each signature records:

- The contributor's GitHub username
- The pull request number where the signature was made
- The timestamp of the signing comment
- The version of the CLA at the time of signing

The dashboard at https://cla-assistant.io shows all signatures for the
linked repository.

## Maintainer / bot exemptions

The CLA Assistant dashboard supports a whitelist of GitHub usernames whose
PRs auto-pass the check. The whitelist is configured in the dashboard, not
in this repository. Currently exempt:

- `cmeans` (maintainer)
- Project bots that open PRs against the repo. The current list lives in
  the cla-assistant dashboard, not here — copying bot names into code is
  a maintenance trap. To see or update the list, sign in to
  cla-assistant.io and open the repository settings.

To update the whitelist, sign in at https://cla-assistant.io, open the
linked repository, and edit the settings.

### Bot bypass mirror (`.github/cla-bot-allowlist`)

CLAassistant silently skips PRs whose diff touches files under
`.github/workflows/**` — its OAuth scope does not include GitHub's
`workflow` scope, so reading the diff fails and the entire run is
aborted. No `license/cla` status is posted, and branch protection then
blocks merge on every workflow-touching bot PR regardless of the
dashboard whitelist.

The repo-local workaround is
[`.github/workflows/cla-bot-bypass.yml`](../.github/workflows/cla-bot-bypass.yml),
which reads [`.github/cla-bot-allowlist`](../.github/cla-bot-allowlist)
on every PR event. When the PR author's login matches an entry, the
workflow posts `license/cla = success` to the PR head with a
description identifying the bypass. Human contributors are not
affected — their PRs still flow through CLAassistant normally.

The allowlist file is the single source of truth for bot bypasses.
To add or remove a bot:

1. Edit `.github/cla-bot-allowlist` — one GitHub login per line;
   comments with `#` allowed.
2. Also update the cla-assistant dashboard whitelist to match (the
   dashboard is still authoritative for the *non-workflow-touching*
   bot PR case where CLAassistant fires normally).

When CLAassistant's OAuth scope eventually covers `workflow`, this
mirror mechanism becomes redundant and the workflow can be removed.

## How to sign (contributor view)

See [`CONTRIBUTING.md`](../CONTRIBUTING.md#how-to-sign).

## Operational notes

- The bot's webhook lives on the repository under
  *Settings → Webhooks*. Removing it disables CLA enforcement.
- Re-linking the repository in the dashboard re-creates the webhook.
- The Gist is public so external auditors and contributors can verify
  signatures without needing repository access.
