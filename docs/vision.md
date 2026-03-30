# Vision: What Knowledge Becomes When It's Ambient

Every app you use knows one thing about you. Your calendar knows your schedule. Your health tracker knows your sleep. Your NAS knows your disk usage. Your AI knows your conversation. But none of them know each other.

The gap isn't between apps — it's between all the fragments of your context that are locked in separate silos. Awareness is a self-hosted service that bridges that gap: knowledge from disconnected contexts accumulates in one store, and your AI agents surface the connections between fragments that no single app can see.

The fragments individually are just data. The connections between them are insight.

## The product is silence

The most important thing Awareness can tell you is nothing.

`attention_needed: false` isn't the absence of information — it's confirmation that nothing requires you right now. The collator checked every alert, evaluated every suppression, matched every pattern, and determined that you can keep doing what you're doing. The [evaluation field](../CHANGELOG.md) makes this tangible: you can see exactly how many things were checked and dismissed.

Every app is designed to pull you in. Awareness might be the first service designed to leave you alone with confidence. The suppression system isn't a feature — it might be the core product. An attention firewall.

## Six things Awareness becomes

### 1. Cross-platform memory

This is what Awareness does today. Tell Claude about your infrastructure on your phone, and Claude Code knows it on your laptop. Draft a plan on mobile during your commute, and your desktop agent picks it up. Knowledge flows between platforms because it lives in a service you control, not in any single app's memory.

### 2. A living estate document

Your partner needs to know about the NAS, the insurance accounts, the Proxmox setup, the property taxes, where the documents are, who the contacts are. If something happened tomorrow, they'd face a wall of unknowns.

But the store is already accumulating this knowledge through daily use. A different view of the same data — filtered by "what would someone need if I weren't here" — becomes a living estate document that's always current, because it maintained itself as a side effect of living your life with an agent. This is an MCP prompt away, not a new feature — the data is already there.

### 3. Place memory

A house has institutional memory that currently lives nowhere. Furnace serviced October. Roof patched 2024. Water heater is eight years old. Breaker panel quirk with the garage circuit.

In Awareness, a source like `home` or a street address accumulates knowledge about a physical space over years. When you sell, the buyer gets something no house has ever come with — a portable knowledge base that explains why the garage outlet trips when the microwave runs. The [export feature](../README.md) on the roadmap makes this transferable.

### 4. Relationship mirror

Not tracking people — noticing patterns you're too close to see.

"The last three times you mentioned [friend], you described them as stressed." "You haven't mentioned [person] in two months" when you used to mention them weekly. Relationships decay silently. No single moment is the one where it happened. The store notices the gradient — entry frequency per person is queryable data, and trend detection surfaces the change.

### 5. Decision archaeology

Six months from now you won't remember why you bought more LXRX in March. The context — pilavapadin Phase 3, the enrollment numbers, the price points — will have faded. Every significant decision has a context halo that decays.

Awareness captures reasoning at the moment of the decision because the agent was part of the conversation and wrote it to the store. "Why did I decide X?" becomes a `get_knowledge` query with a date range, not a memory exercise.

### 6. Community institutional memory

Neighborhood association. Mutual aid network. Volunteer fire department. School parent group. Political campaign. Real operational complexity, zero software budget. Knowledge lives in whoever showed up last time.

A shared Awareness instance becomes institutional memory for an organization running on volunteer labor. Self-hosted means no subscription, no vendor lock-in, no data leaving the community.

## Intentions: goals, not reminders

The next major entry type — `INTENTION` — turns Awareness from a knowledge store into a decision-support system.

An intention isn't "remind me at 5pm." It's a goal with constraints, evaluated against real-world circumstances at the moment it matters.

"Pick up milk" isn't a task — it's a goal: ensure we have milk at home. The fulfillment path depends on circumstances unknown until the moment of evaluation. Is the store open when you'd arrive? Do they have oat milk in stock? Is it cheaper at the place two minutes further? Is there construction on the route?

Awareness evaluates this in two phases:

**Phase 1 (cheap, local):** Am I near a relevant location? Heading in the right direction? Is it the right time? This runs on a lightweight edge device — your phone — and filters out 99% of moments. The edge daemon checks trigger conditions against intentions stored in Awareness and writes a candidate when conditions align.

**Phase 2 (rich, contextual):** Only when Phase 1 triggers. Is the store open at my estimated arrival time? What's the stock pattern like? (Community reports say they're out of oat milk on Sundays.) What's the cost compared to alternatives? Is the route clear? This runs in your conversational agent, which has access to maps, web search, and all the accumulated knowledge in the Awareness store.

The output isn't a notification — it's a recommendation: "Mariano's might be out of oat milk — there've been reports lately. Jewel has it on sale for $2.99 but there's construction on Ashland. Want me to add it to an Instacart order for tomorrow morning instead?"

The service gets smarter over time because the knowledge that feeds evaluation — stock patterns, price history, dietary requirements, route preferences — accumulates in the store through daily use.

## The progression

Today, Awareness is personal — one person's AI tools sharing a single knowledge store. That's where it starts, not where it ends.

**Personal** (now): Your AIs share memory through the Awareness service across every platform. Plan on your phone, implement on your laptop, review from your desktop. Context follows you, not the app.

**Family and trusted circle** (next): A shared Awareness instance for your household. Birthdays, dietary restrictions, who has practice on Thursdays. Knowledge accumulates as family members mention things to their own AIs. No shared spreadsheet to maintain.

**Team** (next): Your team's AIs share operational knowledge through a team Awareness instance. Architecture decisions, coding conventions, on-call runbooks — accumulated through daily work, not documentation sprints. New team member's AI is productive on day one.

**Community** (future): Multiple users with scoped access. Engineering, ops, product — each with their own store, plus cross-team shared knowledge. Volunteer organizations with institutional memory that doesn't walk out the door.

Each step requires trust boundaries — ownership, audit history, edit/view permissions, the ability to revert changes. The changelog tracks history today; full multi-user access control is on the roadmap.

## What makes this different

**Knowledge accumulates through conversation**, not documentation. Nobody stops what they're doing to write things down — the AI does it as part of the work. Edge processes capture it from the tools you already use.

**Your AI reads it automatically.** Unlike a wiki that someone has to remember to check, the Awareness service is consulted at the start of every conversation. The result is a knowledge base that grows as people work, not as a separate task they avoid.

**It flows both ways.** Knowledge doesn't just come in from tools — it goes out. Update a project status in Awareness, and an edge process can reflect it in Notion, Slack, or a commit message. The store becomes the hub, not another silo.

**It's proactive.** Awareness doesn't just store what happened — it evaluates what needs attention. Baseline detection learns what "normal" looks like. Cross-domain inference connects data across sources: bad sleep + packed calendar = maybe reschedule the afternoon meeting.

**It's self-hosted.** Your data stays on your hardware. No subscription, no vendor lock-in, no one else can see it. When the managed service launches, zero-knowledge encryption means even the operator can't access your data.

## Current state

Awareness today is a working personal context service with 29 tools, 5 built-in prompts, custom prompt support, ambient alerting with suppression and pattern matching, semantic search via pgvector + Ollama, intentions with time-based triggers, read/action tracking, entry relationships, and a one-line demo install. It runs on PostgreSQL 17 with auto-healing connections and background embedding generation.

What's described above is the direction — some of it is months away, some further. The architecture is designed to support it: the Store protocol, the collator, the edge provider model, the embedding pipeline, and the evaluation framework are all built with this progression in mind.

## Awareness Canvas

[awareness-canvas](https://github.com/cmeans/awareness-canvas) is a companion project exploring what happens when you give Awareness a visual surface. Instead of building a traditional dashboard, the AI builds it for you — a spatial canvas where you chat with your data, and the agent generates React widgets on demand from awareness queries.

The core idea: your briefing, knowledge explorer, intention tracker, and activity timeline aren't pre-built pages — they're components the AI creates and arranges based on what you ask for. "Show me my infrastructure health" produces a widget. "Add my financial overview next to it" produces another. The canvas remembers your layout.

This is early-stage — planning docs and architecture are in the repo. The data pipeline (Browser → Claude API → self-hosted Awareness + Postgres) is designed so your data never leaves your infrastructure.

## Try it

```bash
curl -sSL https://raw.githubusercontent.com/cmeans/mcp-awareness/main/install-demo.sh | bash
```

See the [README](../README.md) for what's working today.

---

Part of the [<img src="../docs/branding/awareness-logo-32.svg" alt="Awareness logo — a stylized eye with radiating signal lines" height="20"> Awareness](https://github.com/cmeans/mcp-awareness) ecosystem. © 2026 Chris Means
