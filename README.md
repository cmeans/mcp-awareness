# mcp-awareness

[![CI](https://github.com/cmeans/mcp-awareness/actions/workflows/ci.yml/badge.svg)](https://github.com/cmeans/mcp-awareness/actions/workflows/ci.yml)

> **Your AI's memory shouldn't be locked to one app. It should follow you everywhere.**

> [!NOTE]
> This project is evolving fast. See [Current status](#current-status) for what's working and what's planned.

## What this is

`mcp-awareness` is shared memory for every AI you use. Any AI assistant can store and retrieve knowledge through it using the open [Model Context Protocol](https://modelcontextprotocol.io/) (MCP). Self-host it today, or use the managed service when it launches. It works with Claude.ai, Claude Code, Claude Desktop, Claude mobile (Android/iOS), Cursor, and any other MCP-compatible client.

**The problem:** Every AI platform keeps its own memory silo. What you teach Claude doesn't exist in ChatGPT. Your desktop assistant's context doesn't follow you to mobile. Switch platforms, and you start over.

**The fix:** Externalize that knowledge into a service you control. Tell one AI about your infrastructure, your projects, your preferences — and every AI knows it. Permanently, portably, privately.

### What this looks like in practice

<img src="docs/images/android-briefing-demo.png" alt="Claude on Android surfacing an infrastructure alert during an unrelated conversation" width="220" align="right">

This morning, a plan was drafted on Claude Android during a commute. Claude Desktop picked it up and gave engineering feedback that shaped the project roadmap. Claude Code implemented the changes, tested them, and deployed — updating the shared project status so every platform knows what happened. No copy-paste. No "remember what we discussed." The knowledge just flows.

The store also provides ambient system awareness: edge processes report status and alerts, a collation engine applies suppressions and learned patterns, and your AI receives a compact briefing (~200 tokens) at the start of each conversation. If something needs attention, it says so. If not, silence.

<br clear="both">

## How it started

This project began with a single memory instruction in Claude.ai:

> *"On the first turn of each conversation, call `synology-admin:get_resource_usage`. If CPU > 90%, RAM > 85%, any disk > 90% busy, or network/disk I/O looks abnormally high, briefly mention it as an FYI before responding."*

That worked surprisingly well. Infrastructure awareness surfaced inline during unrelated conversations. The AI applied contextual judgment — it knew the NAS was a seedbox, so it didn't flag normal seeding activity. Conversational tuning worked too: "don't bug me until it's 97%" adjusted behavior immediately.

But it had obvious limits. Diagnostics weren't captured at detection time. There was no structural detection — if a key process stopped, every metric looked *better*, and nothing alerted. Knowledge lived in platform-locked memory. It only worked with one system, on one platform.

The [original LinkedIn post](https://www.linkedin.com/posts/cmeans_mcp-modelcontextprotocol-platformengineering-activity-7440439710315098112-Fstj) tells the full story.

`mcp-awareness` is the generalization of that experiment — and it turned out to be bigger than monitoring.

## Core capabilities

### Shared knowledge store

Any AI can write knowledge. Any AI can read it. Knowledge accumulates through conversation, not configuration:

- **`remember`** — store anything worth keeping: personal facts, project notes, skill backups, config snapshots
- **`learn_pattern`** — record operational knowledge with conditions and effects for alert matching
- **`add_context`** — store time-limited knowledge that auto-expires (events, temporary situations)
- **`update_entry`** — modify entries in place with automatic changelog tracking
- **`get_knowledge`** — retrieve by source, tags, or entry type with optional change history

This is the key differentiator from platform-specific memory: the knowledge belongs to *you*, not to Claude, ChatGPT, or any single tool.

### Cross-platform continuity

Every AI you use shares the same knowledge base. Plan on your phone, implement on your laptop, review from your desktop — context follows automatically. Your AIs can also maintain shared project status, so any platform knows what's been done and what's next.

### Ambient system awareness

Edge processes report system status and alerts. The collation engine applies learned patterns and active suppressions, then generates a compact briefing. Your AI checks once at conversation start — if something needs attention, it mentions it; otherwise, silence.

Three layers of detection:

| Layer | Question | Catches |
|-------|----------|---------|
| **Threshold** | "Is this number too high?" | CPU > 90%, disk > 95% full |
| **Baseline** | "Is this abnormal for THIS system?" | Deviation from rolling average |
| **Knowledge** | "Does this match what I expect?" | Process stopped, replication stalled, unexpected quiet |

The third layer is where the value is. Knowledge accumulates through conversation, not YAML.

### Safe data management

Soft delete with 30-day trash retention. Bulk deletes show a dry-run count and require confirmation before committing. Restore from trash at any time. In-place updates track all changes in a changelog. No data is permanently destroyed without a retention period.

### Store introspection

`get_stats` shows entry counts by type and lists all sources — so your AI can decide whether to pull everything or filter first. `get_tags` lists all tags with usage counts, preventing tag drift across platforms (e.g., one AI tagging `"infrastructure"` while another uses `"infra"`).

## Architecture

```mermaid
flowchart TB
    subgraph Clients["Any MCP Client"]
        A1["Claude.ai"]
        A2["Claude Code"]
        A3["Claude Desktop"]
        A4["ChatGPT / Cursor / ..."]
    end

    subgraph Security["Cloudflare Edge"]
        WAF["WAF (path filter)"]
        TLS["TLS + Tunnel"]
    end

    subgraph Edge["Edge Processes"]
        E1["NAS Health Daemon"]
        E2["Calendar Processor"]
        E3["CI/CD Watcher"]
    end

    subgraph Server["mcp-awareness"]
        direction LR
        Store["Store\n(SQLite / Postgres)"]
        Collator["Collator\n• suppressions\n• patterns\n• escalation"]
        Briefing["Briefing\n~200 tokens"]
        Store --> Collator --> Briefing
    end

    Clients <-- "MCP\n(stdio or HTTPS)" --> Security
    Security <--> Server
    Edge -- "report_status\nreport_alert" --> Server
```

## Quick start

### Try the demo (easiest)

One script, three containers, a public URL. No account needed.

```bash
curl -sSL https://raw.githubusercontent.com/cmeans/mcp-awareness/main/install-demo.sh | bash
```

> **Prefer to review the script first?** [View it on GitHub](https://github.com/cmeans/mcp-awareness/blob/main/install-demo.sh), then download and run locally.

This starts the Awareness server, Postgres, and a Cloudflare quick tunnel. You'll get a public URL and ready-to-paste config snippets for Claude.ai, Claude Desktop, Claude Code, Cursor, and VS Code. The instance comes pre-loaded with demo data — your AI will discover it automatically.

> **Note:** The tunnel URL is ephemeral — it changes on restart. For a stable URL, see the [Deployment Guide](docs/deployment-guide.md).

> **Model matters:** Best experience with Claude Sonnet 4.6 or Opus 4.6. Smaller models (Haiku, GPT-4o-mini) may not follow MCP prompts reliably.

### Local development

```bash
git clone https://github.com/cmeans/mcp-awareness.git
cd mcp-awareness
docker compose up -d
```

The server is running on port 8420. Point any MCP client at `http://localhost:8420/mcp`.

### Connect your AI

**Claude Desktop / Claude Code** (local):
```json
{
  "mcpServers": {
    "awareness": {
      "url": "http://localhost:8420/mcp"
    }
  }
}
```

**Claude.ai** (remote, requires [Deployment Guide](docs/deployment-guide.md) setup):
1. Settings → Connectors → Add custom connector
2. Name: `awareness`
3. URL: `https://your-domain.com/your-secret/mcp`
4. Leave OAuth fields blank

### Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `AWARENESS_TRANSPORT` | `stdio` | Transport: `stdio` or `streamable-http` |
| `AWARENESS_HOST` | `0.0.0.0` | Bind address (HTTP mode) |
| `AWARENESS_PORT` | `8420` | Port (HTTP mode) |
| `AWARENESS_DATA_DIR` | `./data` | SQLite database directory |
| `AWARENESS_BACKEND` | `sqlite` | Storage backend: `sqlite` or `postgres` |
| `AWARENESS_DATABASE_URL` | _(none)_ | PostgreSQL connection string (required when backend is `postgres`). Example: `postgresql://user:pass@localhost:5432/awareness` |
| `AWARENESS_MOUNT_PATH` | _(none)_ | Secret path prefix for access control (e.g., `/my-secret`). When set, only `/<secret>/mcp` is served; all other paths return 404. Use with a Cloudflare WAF rule. |

### Development

```bash
pip install -e ".[dev]"    # install with dev dependencies
python -m pytest tests/    # run tests
ruff check src/ tests/     # lint
mypy src/mcp_awareness/    # type check
```

## Tools

The server exposes 18 MCP tools. Clients that support MCP resources also get 6 read-only resources, but since many clients (including Claude.ai) only surface tools, every resource has a tool mirror.

### Read tools

| Tool | Description |
|------|-------------|
| `get_briefing` | Compact awareness summary (~200 tokens all-clear, ~500 with issues). Call at conversation start. Pre-filtered through patterns and suppressions. |
| `get_alerts` | Active alerts, optionally filtered by source. Drill-down from briefing. |
| `get_status` | Full status for a specific source including metrics and inventory. |
| `get_knowledge` | Knowledge entries (patterns, context, preferences, notes). Filter by source, tags, entry_type. `include_history` controls changelog visibility. |
| `get_suppressions` | Active alert suppressions with expiry times and escalation settings. |
| `get_stats` | Entry counts by type, list of sources, total count. Call before `get_knowledge` to decide whether to filter. |
| `get_tags` | All tags in use with usage counts. Use to discover existing tags and prevent drift. |

### Write tools

| Tool | Description |
|------|-------------|
| `report_status` | Report system status. Called periodically by edge processes. Upserts one entry per source; stale if TTL expires without refresh. |
| `report_alert` | Report or resolve an alert. Captures diagnostics at detection time. Levels: `warning`, `critical`. Types: `threshold`, `structural`, `baseline`. |
| `learn_pattern` | Record an operational pattern with conditions/effects for alert matching. Set `learned_from` to your platform. |
| `remember` | Store a general-purpose note. Optional `content` payload with MIME `content_type`. For anything that isn't an operational pattern or time-limited context. |
| `add_context` | Record time-limited knowledge (default 30 days). Use for events, temporary situations, or facts that lose relevance. |
| `set_preference` | Set a portable presentation preference (e.g., `alert_verbosity`, `check_frequency`). Upserts by key + scope. |
| `suppress_alert` | Suppress alerts by source/tags/metric. Time-limited with escalation override — critical alerts can break through. |

### Data management tools

| Tool | Description |
|------|-------------|
| `update_entry` | Update a knowledge entry in place (note, pattern, context, preference). Tracks changes in `changelog`. Status/alert/suppression are immutable. |
| `delete_entry` | Soft-delete entries (30-day trash). By ID, by source + type, or by source. Bulk deletes require `confirm=True` (dry-run by default). |
| `restore_entry` | Restore a soft-deleted entry from trash. |
| `get_deleted` | List all entries in trash with IDs for restore. |

See the [Data Dictionary](docs/data-dictionary.md) for full schema documentation.

## Security

The awareness store may contain personal information. Securing the endpoint is not optional. The current approach uses two layers:

1. **Cloudflare WAF** — blocks requests at the edge if the URL path doesn't match the secret prefix. Unauthorized traffic never reaches your machine.
2. **Server middleware** — strips the secret prefix and routes to `/mcp`. Requests without it get 404.

See [Security considerations](docs/deployment-guide.md#security-considerations) in the Deployment Guide for details, limitations, and what's planned.

## Current status

**Working end-to-end** — deployed on `mcpawareness.com` via Cloudflare Tunnel with WAF protection. Tested with Claude.ai, Claude Code, Claude Desktop, Claude mobile (Android), and Cursor.

**Implemented:**
- Shared knowledge store: `remember`, `learn_pattern`, `add_context`, `set_preference` with filtered retrieval
- Idempotent upserts via `logical_key` — same source + key updates in place with changelog tracking, no UUID needed
- In-place updates with changelog tracking (`update_entry` + `include_history`)
- Store introspection: `get_stats` for entry counts, `get_tags` for tag discovery
- General-purpose notes with optional content payload and MIME type
- Ambient awareness: status reporting, alert detection, suppression, briefing generation
- PostgreSQL backend with pgvector (production default), TIMESTAMPTZ columns, GIN-indexed tag queries, Debezium CDC-ready (`wal_level=logical`)
- SQLite backend available as lightweight alternative
- Storage abstraction: `Store` protocol — backends are swappable without changing server or collator logic
- Full MCP API: 6 resources + 18 tools + 5 prompts (read mirrors for tools-only clients like Claude.ai)
- Soft delete with 30-day trash, dry-run confirmation for bulk operations
- Request timing instrumentation and `/health` endpoint for latency analysis
- Streamable HTTP + stdio transports
- Secret path auth + Cloudflare WAF for edge-level access control
- Docker Compose with Postgres, named Cloudflare Tunnel, or ephemeral quick tunnel
- Three-layer detection model (threshold + knowledge implemented; baseline planned)
- Suppression system with time-based expiry and escalation overrides
- Alembic migration framework for PostgreSQL (version-tracked, raw SQL, auto-runs on Docker startup)
- Pagination (`limit`/`offset`) on knowledge, alerts, entries, and trash queries
- QA gate: `QA Approved` label required to merge PRs (pending status, not failed)
- MCP Prompts: 5 built-in dynamic prompts + user-defined prompts from store entries with `{{var}}` templates
- 181 tests, strict type checking, CI pipeline

**Not yet implemented:**
- Layer 2 (baseline) detection — rolling averages and deviation calculation
- Edge processes — no automated producers yet ([example script](examples/simulate_edge.py) demonstrates the write path)
- Semantic search — current knowledge retrieval is tag/keyword-based; vector similarity is planned
- OAuth / API key authentication — current auth is secret-path-based; proper token auth requires MCP client support for auth flows

## Vision

Today, `mcp-awareness` is personal — one person's AI tools sharing a single knowledge store. That's where it starts, not where it ends.

### Personal → Team → Organization

**Personal** (now): Your AIs share memory across every platform you use. Plan on your phone, implement on your laptop, review from your desktop. Context follows you, not the app.

**Family & trusted circle** (next): A shared store for your household and the people in your life. Birthdays, wishlists, dietary restrictions, who's allergic to what, which kid has practice on Thursdays. "Plan a birthday dinner for Mom" and your AI already knows the date, her favorite restaurant, and who's in town. No shared spreadsheet to maintain — knowledge accumulates as family members mention things to their own AIs. Share specific notes or collections with people outside the household — change the door code while you're traveling, and the dog walker's AI surfaces the update on the way to your house.

**Team** (next): A shared awareness store for your team at work. Your AI knows the on-call runbook, the architecture decisions, the coding conventions — not because someone wrote a doc, but because the team's AIs have been accumulating knowledge through daily work. New team member's AI is productive on day one. Temporary context like "code freeze until Thursday" is automatically known by everyone's AI and automatically forgotten when it lifts.

**Organization** (future): Multiple teams, scoped access. Engineering, ops, product — each with their own store, plus cross-team shared knowledge.

Shared stores require trust boundaries — ownership, audit history, edit/view permissions, the ability to revert changes. The `changelog` is a starting point; full multi-user access control is on the roadmap alongside OAuth and the managed service.

### Universal context, not just monitoring

Awareness started as a system monitoring tool, but that's just one source of context. The real vision is broader: every tool you use feeds knowledge into every AI you use.

Take meeting notes in Notion — an edge process summarizes them and stores them in awareness. Next time you open Claude Code to implement what was discussed, it already knows the decisions and context. Update a ticket in Linear, and your AI knows the priority changed. Merge a PR in GitHub, and every platform knows the feature shipped.

Notion, Slack, Linear, Jira, Google Docs, health trackers, calendars, infrastructure monitors — any tool with an API becomes a source.

And it flows both ways. Knowledge doesn't just come *in* — it goes *out*. Update a memory prompt in awareness, and an edge process auto-commits the change to your GitHub docs. Change your project status, and it shows up in Notion or Slack without anyone posting. Awareness becomes the hub, not a silo — your AI is the integration layer between everything you use, with context that flows automatically in both directions.

### What makes this different from a wiki

Knowledge accumulates through conversation and work, not documentation. Nobody has to stop what they're doing to write things down — the AI does it as part of the work, and edge processes capture it from the tools you already use. Unlike a wiki that someone has to remember to check, your AI reads from awareness automatically. The result is a living knowledge base that grows as people work, not as a separate task they avoid.

### Proactive intelligence

The system doesn't just store what happened — it helps you decide what to do about it. Baseline detection learns what "normal" looks like and flags deviations. Cross-domain inference connects data across sources: bad sleep from your health tracker + a packed calendar = a recommendation to reschedule the afternoon meeting. An alert from your infrastructure + context from last week's incident = "this looks like the same root cause."

## Design docs

- [Deployment Guide](docs/deployment-guide.md) — deployment walkthrough with Cloudflare Tunnel, WAF, and Claude.ai integration
- [From Metrics to Mental Models](docs/from-metrics-to-mental-models.md) — core spec: three-layer detection model, API design, data schema
- [Collation Layer](docs/collation-layer.md) — briefing resource, token optimization, escalation logic
- [Data Dictionary](docs/data-dictionary.md) — database schema, entry types, data field structures, lifecycle rules
- [Memory Prompts](docs/memory-prompts.md) — how to configure your AI to use awareness (platform memory, global CLAUDE.md, project CLAUDE.md)
- [Changelog](CHANGELOG.md) — version history

## What's different

| | mcp-awareness | Platform memory (Claude, ChatGPT) | Mem0 / Zep |
|---|---|---|---|
| **Portable** | Any MCP client | Locked to one platform | Framework-specific API |
| **Self-hosted** | Yes, with managed option planned | No | SaaS only (Mem0) |
| **Bidirectional** | Read and write from any client | Read-only recall | Varies |
| **Change tracking** | `changelog` on every update | None | None |
| **Open protocol** | MCP (open standard) | Proprietary | Proprietary |
| **Awareness** | Knowledge + system monitoring | Memory only | Memory only |
| **You own the data** | Yes | No | Depends |

## How it's built

This project is built using the thing it builds. Multiple AI instances across platforms — Claude.ai, Claude Code, Claude Desktop, Claude mobile — collaborate through awareness itself, and the friction they encounter drives the features they propose.

### The feedback loop in action

**Feature discovery through friction.** Claude Desktop ran a code review of the mcp-awareness codebase and tried to update an existing review entry. It couldn't — `update_entry` requires a UUID, and Desktop didn't know the UUID from the entry Claude.ai had created in a different session. The workaround was creating a duplicate with a "supersedes" note. Desktop recognized this as exactly the kind of data pollution awareness should prevent, designed a solution ([`logical_key` upsert](https://github.com/cmeans/mcp-awareness/pull/18)), and stored the full proposal in awareness. Claude Code discovered it, implemented it, and shipped it — all through the shared store.

**Prompt tuning through data audit.** The first audit of stored data found 53 out of 56 `learn_pattern` entries had empty conditions/effects — they should have been notes. Tag drift was rampant: `infrastructure` vs `infra`, `torrent` vs `torrents`. Source naming was chaotic: `chris-personal`, `chris-career`, `chris-health` instead of one `personal` source with domain tags. Each finding led to a prompt update. The prompts got more explicit, the naming conventions got documented, and the next round of data was cleaner.

**Cross-platform planning.** A health data integration plan was drafted on Claude mobile during a commute, stored in awareness, and picked up by Claude Code for implementation — no copy-paste, no "remember what we discussed." The context just followed.

**Agent-driven code review.** Claude Desktop reviewed the awareness tools as a consumer and gave engineering feedback: filtered queries needed to reduce token cost, error messages were opaque, tag matching had data model inconsistencies. Every suggestion was actionable, and most shipped within hours.

The collaboration model itself is what this project explores: AI that builds up shared knowledge through work rather than configuration. The awareness service is a formalization of how that collaboration already works — just extended to everything.

## License

Apache 2.0 — see [LICENSE](LICENSE) for details.

---

Copyright (c) 2026 Chris Means
