# Case Studies

Real-world examples of awareness in practice — what worked, what broke, and what we learned. Each case study is attributed to the agent and platform involved, but every step was human-directed: the user asked the questions, made the decisions, reviewed the output, and approved the results. The agents are tools, not autonomous actors.

---

<details>
<summary><strong>Patterns over events</strong> — OS update analysis (Claude on claude.ai)</summary>

### Patterns over events — OS update analysis

**Platform:** Claude on claude.ai
**When:** January–March 2026
**Principle:** Store knowledge that transfers, not event logs

Over two months, Claude on claude.ai processed 24+ Fedora OS update analyses — parsing dnf output, flagging risks, advising on reboots. None of these had been saved to awareness. The user asked whether they should be.

**The decision: don't store individual update events. Extract the recurring operational patterns instead.**

Three reasons:

1. **Conversation search already works.** A keyword search across past chats retrieved 20+ update records in seconds, with full context — package lists, version numbers, warnings, next steps. Awareness would duplicate what's already queryable.

2. **Update events go stale immediately.** "Kernel 6.19.6 installed on March 12" is only true until the next kernel update. Storing ephemeral facts as permanent knowledge creates noise.

3. **Patterns are durable; events are not.** Across 24 updates, three operational patterns kept recurring. Those are genuinely useful for any agent working on the system — they encode "when X happens, expect Y" knowledge that saves time and prevents mistakes.

#### What was stored (3 learned patterns)

1. **Mesa / RPM Fusion lag** — When Fedora bumps mesa, RPM Fusion's `mesa-va-drivers-freeworld` frequently hasn't caught up, blocking the upgrade. Resolution: wait 1–3 days, don't force-remove (loses hardware video decode).

2. **VirtualBox kmod orphaned on kernel upgrade** — Every kernel upgrade removes the old kmod-VirtualBox. The new one isn't auto-installed. Fix: `sudo akmods --force` after reboot, or wait for RPM Fusion. RPM scriptlet warnings during removal are cosmetic.

3. **GNOME Software phantom notifications** — After dnf applies updates requiring reboot, the GNOME Software tray shows "updates available." It's the same packages, not new ones. Clears after reboot.

#### Why it matters

- **Claude on claude.ai** already has the patterns next time a Mesa update is held back — no re-derivation needed.
- **Claude Code** doing system maintenance can query awareness and find the VirtualBox fix without the user explaining it.
- **Any future agent** on any platform inherits two months of operational learning on day one.

**The principle:** Awareness is for knowledge that transfers — patterns, decisions, preferences, operational rules. If the data goes stale with the next occurrence, it belongs in conversation history. If it teaches something reusable, it belongs in awareness.

</details>

---

<details>
<summary><strong>Feature discovery through friction</strong> — logical_key upsert (Claude Desktop → Claude Code)</summary>

### Feature discovery through friction — logical_key upsert

**Discovered with:** Claude Desktop
**Implemented with:** Claude Code
**When:** March 2026 ([PR #18](https://github.com/cmeans/mcp-awareness/pull/18))
**Principle:** The friction encountered in daily use drives the features that get built

Claude Desktop ran a code review of the mcp-awareness codebase and tried to update an existing review entry. It couldn't — `update_entry` requires a UUID, and Desktop didn't know the UUID from the entry Claude.ai had created in a different session.

The workaround was creating a duplicate with a "supersedes" note. Through discussion, Desktop helped the user design a solution (`logical_key` upsert), and the proposal was stored in awareness.

When the user moved to Claude Code, the proposal was already in the shared store — no copy-paste needed. The user directed implementation, reviewed the code, and shipped it.

#### Why it matters

One user, three platforms, one feature — discovered through friction, designed collaboratively with the agent that hit the problem, implemented with the agent best suited for coding. The shared knowledge store was both the communication channel and the subject of the improvement.

</details>

---

<details>
<summary><strong>Prompt tuning through data audit</strong> — 53/56 entries misclassified (Claude on claude.ai)</summary>

### Prompt tuning through data audit

**Platform:** Claude on claude.ai
**When:** March 2026
**Principle:** Audit your data, not just your code

The first audit of stored data found 53 out of 56 `learn_pattern` entries had empty conditions/effects — they should have been notes. Tag drift was rampant: `infrastructure` vs `infra`, `torrent` vs `torrents`. Source naming was chaotic: `chris-personal`, `chris-career`, `chris-health` instead of one `personal` source with domain tags.

The user reviewed each finding and directed prompt updates. The prompts got more explicit, the naming conventions got documented, and the next round of data was cleaner.

#### Why it matters

The quality of a knowledge store depends on the quality of what goes in. Agent prompts are the input filter. Auditing the data — with the agent surfacing the patterns and the user deciding what to fix — revealed prompt gaps that would have been invisible from code review alone.

</details>

---

<details>
<summary><strong>Cross-platform planning</strong> — commute to deployment (Claude Android → Desktop → Code)</summary>

### Cross-platform planning

**Drafted with:** Claude on Android (claude.ai mobile)
**Refined with:** Claude Desktop
**Implemented with:** Claude Code
**When:** March 2026
**Principle:** Context follows you, not the other way around

The user drafted a health data integration plan with Claude on mobile during a commute, storing it in awareness. Back at the desk, Claude Desktop already had the context and helped refine the engineering approach. The user then moved to Claude Code for implementation, testing, and deployment — updating shared project status so every platform knows what happened.

No copy-paste. No "remember what we discussed." The knowledge just followed the user.

#### Why it matters

This is the core value proposition in action. One user, three platforms, three contexts (commute, desk, terminal), one continuous thread of work. The awareness store carried the context so the user didn't have to.

</details>

---

<details>
<summary><strong>Agent-driven code review</strong> — tool surface feedback (Claude Desktop)</summary>

### Agent-driven code review

**Platform:** Claude Desktop
**When:** March 2026
**Principle:** Your best beta testers are your own agents

The user asked Claude Desktop to review the awareness tools as a consumer. The feedback was specific: filtered queries needed to reduce token cost, error messages were opaque, tag matching had data model inconsistencies. The user evaluated each suggestion and directed the fixes — most shipped within hours.

#### Why it matters

The agent consuming the API is uniquely positioned to surface friction — it experiences the rough edges that human code review misses. But the user decides what's actionable. Desktop's feedback, filtered through the user's judgment, led to query optimizations, better error messages, and data model fixes that improved the experience for every connected agent.

</details>

---

<details>
<summary><strong>Query discipline</strong> — the 356K blowout (Claude Code)</summary>

### Query discipline — the 356K blowout

**Platform:** Claude Code
**When:** March 2026 ([PR #54](https://github.com/cmeans/mcp-awareness/pull/54))
**Principle:** Teach clients how to query, not just what to query

During a routine QA review, Claude Code called `get_knowledge` with a broad tag filter and no `limit`. The result: 356,751 characters — exceeding the tool output limit and forcing the response to be saved to a temp file. The user spotted the problem, diagnosed the root cause, and directed the fix.

The solution was two-fold:
1. **Query discipline guidance** added to the MCP server instructions — all clients now receive guidance to use `mode='list'` first, always set `limit`, use `hint` for relevance ranking, and narrow with specific tags.
2. **String externalization** — the instructions were moved from an inline Python string to `instructions.md`, establishing content/code separation for maintainability and future i18n.

#### Why it matters

The server can't control how clients query, but it can teach them. By embedding query discipline in the MCP instructions, every connected client — on every platform — receives the same guidance. The 356K blowout was a one-time learning event; the server instructions ensure it doesn't recur.

</details>

---

<details>
<summary><strong>Aspirational README audit</strong> — catching our own overpromises (Claude Code Dev + QA roles)</summary>

### Aspirational README audit — catching our own overpromises

**Platform:** Claude Code (Dev role)
**Reviewed with:** Claude Code (QA role)
**When:** March 2026 ([PR #53](https://github.com/cmeans/mcp-awareness/pull/53))
**Principle:** Ground your documentation in what works today

During a PR review, the user asked Claude Code (in its Developer role) to review the README for technical accuracy. It flagged three claims that described capabilities not yet built: a doctor appointment scenario requiring calendar/transit/weather/geofence integration, an intentions example requiring GPS-based geofencing, and a "today" summary listing unbuilt edge capabilities.

The user confirmed these were valid concerns and directed the fixes. Claude Code (in its QA role) independently verified the changes and noted a CHANGELOG categorization nit, which the user also had corrected.

The result replaced aspirational scenarios with grounded examples of what actually works, while keeping the vision in the Vision section where it belongs.

#### Why it matters

Documentation that overpromises erodes trust — especially when visitors are evaluating whether to adopt. Having agents review documentation for accuracy, not just code for correctness, catches a class of problems that traditional CI can't detect. The two-role pattern (Dev writes, QA reviews) provided an additional layer of scrutiny.

</details>

---

Part of the [<img src="../docs/branding/awareness-logo-32.svg" alt="Awareness logo — a stylized eye with radiating signal lines" height="20"> Awareness](https://github.com/cmeans/mcp-awareness) ecosystem. © 2026 Chris Means
