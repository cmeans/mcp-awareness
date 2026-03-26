# Case Studies

Real-world examples of awareness in practice — what worked, what broke, and what we learned. Each case study is attributed to the agent and platform that drove the discovery.

---

## Patterns over events — OS update analysis

**Discovered by:** Claude on claude.ai
**When:** January–March 2026
**Principle:** Store knowledge that transfers, not event logs

Over two months, Claude on claude.ai processed 24+ Fedora OS update analyses — parsing dnf output, flagging risks, advising on reboots. None of these had been saved to awareness. The user asked whether they should be.

**The decision: don't store individual update events. Extract the recurring operational patterns instead.**

Three reasons:

1. **Conversation search already works.** A keyword search across past chats retrieved 20+ update records in seconds, with full context — package lists, version numbers, warnings, next steps. Awareness would duplicate what's already queryable.

2. **Update events go stale immediately.** "Kernel 6.19.6 installed on March 12" is only true until the next kernel update. Storing ephemeral facts as permanent knowledge creates noise.

3. **Patterns are durable; events are not.** Across 24 updates, three operational patterns kept recurring. Those are genuinely useful for any agent working on the system — they encode "when X happens, expect Y" knowledge that saves time and prevents mistakes.

### What was stored (3 learned patterns)

1. **Mesa / RPM Fusion lag** — When Fedora bumps mesa, RPM Fusion's `mesa-va-drivers-freeworld` frequently hasn't caught up, blocking the upgrade. Resolution: wait 1–3 days, don't force-remove (loses hardware video decode).

2. **VirtualBox kmod orphaned on kernel upgrade** — Every kernel upgrade removes the old kmod-VirtualBox. The new one isn't auto-installed. Fix: `sudo akmods --force` after reboot, or wait for RPM Fusion. RPM scriptlet warnings during removal are cosmetic.

3. **GNOME Software phantom notifications** — After dnf applies updates requiring reboot, the GNOME Software tray shows "updates available." It's the same packages, not new ones. Clears after reboot.

### Why it matters

- **Claude on claude.ai** already has the patterns next time a Mesa update is held back — no re-derivation needed.
- **Claude Code** doing system maintenance can query awareness and find the VirtualBox fix without the user explaining it.
- **Any future agent** on any platform inherits two months of operational learning on day one.

**The principle:** Awareness is for knowledge that transfers — patterns, decisions, preferences, operational rules. If the data goes stale with the next occurrence, it belongs in conversation history. If it teaches something reusable, it belongs in awareness.

---

## Feature discovery through friction — logical_key upsert

**Discovered by:** Claude Desktop
**Implemented by:** Claude Code
**When:** March 2026 ([PR #18](https://github.com/cmeans/mcp-awareness/pull/18))
**Principle:** The friction agents encounter drives the features they propose

Claude Desktop ran a code review of the mcp-awareness codebase and tried to update an existing review entry. It couldn't — `update_entry` requires a UUID, and Desktop didn't know the UUID from the entry Claude.ai had created in a different session.

The workaround was creating a duplicate with a "supersedes" note. Desktop recognized this as exactly the kind of data pollution awareness should prevent, designed a solution (`logical_key` upsert), and stored the full proposal in awareness.

Claude Code discovered the proposal in the shared store, implemented it, and shipped it — all without any copy-paste or "remember what we discussed."

### Why it matters

Three agents, three platforms, one feature — discovered through friction, designed by the agent that hit the problem, implemented by the agent best suited for coding. The shared knowledge store was both the communication channel and the subject of the improvement.

---

## Prompt tuning through data audit

**Discovered by:** Claude on claude.ai
**When:** March 2026
**Principle:** Audit your data, not just your code

The first audit of stored data found 53 out of 56 `learn_pattern` entries had empty conditions/effects — they should have been notes. Tag drift was rampant: `infrastructure` vs `infra`, `torrent` vs `torrents`. Source naming was chaotic: `chris-personal`, `chris-career`, `chris-health` instead of one `personal` source with domain tags.

Each finding led to a prompt update. The prompts got more explicit, the naming conventions got documented, and the next round of data was cleaner.

### Why it matters

The quality of a knowledge store depends on the quality of what goes in. Agent prompts are the input filter. Auditing the data revealed prompt gaps that would have been invisible from code review alone.

---

## Cross-platform planning

**Drafted by:** Claude on Android (claude.ai mobile)
**Refined by:** Claude Desktop
**Implemented by:** Claude Code
**When:** March 2026
**Principle:** Context follows you, not the other way around

A health data integration plan was drafted on Claude mobile during a commute, stored in awareness, and picked up by Claude Desktop for engineering feedback that shaped the project roadmap. Claude Code then implemented the changes, tested them, and deployed — updating the shared project status so every platform knows what happened.

No copy-paste. No "remember what we discussed." The knowledge just followed.

### Why it matters

This is the core value proposition in action. Three platforms, three contexts (commute, desk, terminal), one continuous thread of work. The awareness store replaced the human as the message bus.

---

## Agent-driven code review

**Performed by:** Claude Desktop
**When:** March 2026
**Principle:** Your best beta testers are your own agents

Claude Desktop reviewed the awareness tools as a consumer and gave engineering feedback: filtered queries needed to reduce token cost, error messages were opaque, tag matching had data model inconsistencies. Every suggestion was actionable, and most shipped within hours.

### Why it matters

The agent consuming the API is uniquely positioned to evaluate it — it experiences the friction that human code review misses. Desktop's feedback led to query optimizations, better error messages, and data model fixes that improved the experience for every connected agent.

---

## Query discipline — the 356K blowout

**Discovered by:** Claude Code
**When:** March 2026 ([PR #54](https://github.com/cmeans/mcp-awareness/pull/54))
**Principle:** Teach clients how to query, not just what to query

During a routine QA review, Claude Code called `get_knowledge` with a broad tag filter and no `limit`. The result: 356,751 characters — exceeding the tool output limit and forcing the response to be saved to a temp file.

The fix was two-fold:
1. **Query discipline guidance** added to the MCP server instructions — all clients now receive guidance to use `mode='list'` first, always set `limit`, use `hint` for relevance ranking, and narrow with specific tags.
2. **String externalization** — the instructions were moved from an inline Python string to `instructions.md`, establishing content/code separation for maintainability and future i18n.

### Why it matters

The server can't control how clients query, but it can teach them. By embedding query discipline in the MCP instructions, every connected client — on every platform — receives the same guidance. The 356K blowout was a one-time learning event; the server instructions ensure it doesn't recur.

---

## Aspirational README audit — catching our own overpromises

**Discovered by:** Claude Code (Dev role)
**Reviewed by:** Claude Code (QA role)
**When:** March 2026 ([PR #53](https://github.com/cmeans/mcp-awareness/pull/53))
**Principle:** Ground your documentation in what works today

During a PR review, Claude Code (in its Developer role) flagged three README claims that described capabilities not yet built: a doctor appointment scenario requiring calendar/transit/weather/geofence integration, an intentions example requiring GPS-based geofencing, and a "today" summary listing unbuilt edge capabilities.

Claude Code (in its QA role) independently confirmed the findings and noted a CHANGELOG categorization nit.

The fix replaced aspirational scenarios with grounded examples of what actually works, while keeping the vision in the Vision section where it belongs.

### Why it matters

Documentation that overpromises erodes trust — especially when visitors are evaluating whether to adopt. Having agents review documentation for accuracy, not just code for correctness, catches a class of problems that traditional CI can't detect. The two-role pattern (Dev writes, QA reviews) provided an additional layer of scrutiny.
