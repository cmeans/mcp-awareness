<p align="center">
  <img src="docs/branding/awareness-logo-wordmark.svg" alt="awareness" width="400">
</p>

<p align="center">
  <a href="https://github.com/cmeans/mcp-awareness/actions/workflows/ci.yml"><img src="https://github.com/cmeans/mcp-awareness/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="https://codecov.io/gh/cmeans/mcp-awareness"><img src="https://codecov.io/gh/cmeans/mcp-awareness/branch/main/graph/badge.svg" alt="codecov"></a>
  <a href="https://www.python.org/downloads/"><img src="https://img.shields.io/badge/python-3.10%2B-blue" alt="Python 3.10+"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-Apache%202.0-blue" alt="License"></a>
  <a href="https://ghcr.io/cmeans/mcp-awareness"><img src="https://img.shields.io/badge/docker-ghcr.io-blue" alt="Docker"></a>
</p>

> **Your AI's memory shouldn't be locked to one app. It should follow you everywhere.**

> [!NOTE]
> Early-stage but actively deployed — 333+ tests, 12+ releases, in daily use across Claude.ai, Claude Code, and Claude Desktop. See [Current status](#current-status) for what's working and what's planned.

## What this is

`mcp-awareness` is shared memory for every AI you use. Any AI assistant can store and retrieve knowledge through it using the open [Model Context Protocol](https://modelcontextprotocol.io/) (MCP). Self-host it today, or use the managed service when it launches. It works with any MCP-compatible client — Claude (all platforms), Cursor, VS Code, and more.

**The problem:** Every AI platform keeps its own memory silo. What you teach Claude doesn't exist in ChatGPT. Your desktop assistant's context doesn't follow you to mobile. Switch platforms, and you start over.

**The fix:** Externalize that knowledge into a service you control. Tell one AI about your infrastructure, your projects, your preferences — and every AI knows it. Permanently, portably, privately.

### What this looks like in practice

<img src="docs/images/android-briefing-demo.png" alt="Claude on Android surfacing an awareness briefing" width="220" align="right">

This morning, you drafted a plan with Claude on your phone during a commute. Back at your desk, Claude Desktop already had the context — you refined the engineering approach together. You moved to Claude Code for implementation and deployment, updating shared project status so every platform knows what happened. No copy-paste. No "remember what we discussed." The knowledge just follows you.

Tell one AI about your infrastructure, your preferences, or a design decision — and every AI knows it. Correct a mistake on your phone, and your desktop assistant never repeats it. That cross-platform continuity works today.

The store also provides ambient awareness: a collation engine applies learned patterns and suppressions, and your AI receives a compact briefing (~200 tokens) at the start of each conversation. If something needs attention, it says so. If not, silence — and that silence is the product.

<br clear="both">

## How it started

What started as a homelab monitoring experiment turned into a portable knowledge layer for your entire life. The [original LinkedIn post](https://www.linkedin.com/posts/cmeans_mcp-modelcontextprotocol-platformengineering-activity-7440439710315098112-Fstj) tells the origin story.

<details>
<summary>The full origin story</summary>

This project began with a single memory instruction in Claude.ai:

> *"On the first turn of each conversation, call `synology-admin:get_resource_usage`. If CPU > 90%, RAM > 85%, any disk > 90% busy, or network/disk I/O looks abnormally high, briefly mention it as an FYI before responding."*

That worked surprisingly well. Infrastructure awareness surfaced inline during unrelated conversations. The AI applied contextual judgment — it knew the NAS was a seedbox, so it didn't flag normal seeding activity. Conversational tuning worked too: "don't bug me until it's 97%" adjusted behavior immediately.

But it had obvious limits. Diagnostics weren't captured at detection time. There was no structural detection — if a key process stopped, every metric looked *better*, and nothing alerted. Knowledge lived in platform-locked memory. It only worked with one system, on one platform.

</details>

Today it's a portable knowledge store that tracks personal facts, project history, design decisions, and intentions alongside infrastructure monitoring. As edge providers come online, it will extend to calendars, location, health, and more — but the core store and cross-platform continuity work now.

## Core capabilities

### Shared knowledge store

Any AI can write knowledge. Any AI can read it. Knowledge accumulates through conversation, not configuration:

- **`remember`** — store permanent knowledge (still true in 30 days?): personal facts, project notes, design decisions
- **`add_context`** — store temporal knowledge that auto-expires (happening now, will become stale?)
- **`learn_pattern`** — record if/then rules for alert matching (when X happens, expect Y)
- **`update_entry`** — modify entries in place with automatic changelog tracking
- **`get_knowledge`** — retrieve by source, tags, or entry type with optional change history

This is the key differentiator from platform-specific memory: the knowledge belongs to *you*, not to Claude, ChatGPT, or any single tool.

### Cross-platform continuity

Every AI you use shares the same knowledge base. Plan on your phone, implement on your laptop, review from your desktop — context follows automatically. Your AIs can also maintain shared project status, so any platform knows what's been done and what's next.

### Intentions — todos that manage themselves

Create a todo, reminder, or planned action from any platform. Intentions have a lifecycle — pending → fired → active → completed — and agents advance them through conversation. Time-based intentions fire automatically at `deliver_at` timestamps. As signal sources come online (GPS, calendar), intentions will also fire based on location and context.

### Knowledge that outlives you

Over time, your awareness store becomes a living estate document — financial accounts, insurance details, medical info, system access, family contacts. Not because you sat down to write a manual, but because you asked your AI to remember important details as you mentioned them. If you weren't here tomorrow, someone could find what they need.

### Ambient awareness

Your AI receives a compact briefing (~200 tokens) at the start of each conversation. The collation engine applies learned patterns and active suppressions, then decides what to surface. If something needs attention, it mentions it. Otherwise, silence — and that silence is the product, confirming that everything was checked and nothing needs you.

For infrastructure monitoring, three layers of detection apply:

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
        A4["Cursor / VS Code / ..."]
    end

    subgraph Security["Cloudflare Edge"]
        WAF["WAF (path filter)"]
        TLS["TLS + Tunnel"]
    end

    subgraph Signals["Signal Sources (planned)"]
        direction TB
        S1["📅 Calendar"]
        S2["📍 GPS / Location"]
        S3["💬 SMS / Messaging"]
        S4["🏠 Home Assistant"]
        S5["🖥️ NAS / Docker / CI"]
    end

    subgraph Server["mcp-awareness"]
        direction LR
        Store["Store\n(Postgres + pgvector)"]
        Collator["Collator\n• patterns\n• suppressions\n• intentions"]
        Briefing["Briefing\n~200 tokens"]
        Store --> Collator --> Briefing
    end

    Clients <-- "MCP\n(stdio or HTTPS)" --> Security
    Security <--> Server
    Signals -- "remember\nadd_context\nremind" --> Server
```

## Quick start

### Try the demo (easiest)

One script, three containers, a public URL. No account needed.

```bash
curl -sSL https://raw.githubusercontent.com/cmeans/mcp-awareness/main/install-demo.sh | bash
```

> **Prefer to review the script first?** [View it on GitHub](https://github.com/cmeans/mcp-awareness/blob/main/install-demo.sh), then download and run locally.

This starts the Awareness server, Postgres, and a Cloudflare quick tunnel. You'll get a public URL and ready-to-paste config snippets for all major MCP clients. The instance comes pre-loaded with demo data — your AI will discover it automatically.

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

#### Server

| Variable | Default | Description |
|----------|---------|-------------|
| `AWARENESS_TRANSPORT` | `stdio` | Transport: `stdio` or `streamable-http` |
| `AWARENESS_HOST` | `0.0.0.0` | Bind address (HTTP mode) |
| `AWARENESS_PORT` | `8420` | Port (HTTP mode) |
| `AWARENESS_DATABASE_URL` | _(required)_ | PostgreSQL connection string. Example: `postgresql://user:pass@localhost:5432/awareness` |
| `AWARENESS_MOUNT_PATH` | _(none)_ | Secret path prefix for access control (e.g., `/my-secret`). When set, only `/<secret>/mcp` is served; all other paths return 404. Use with a Cloudflare WAF rule. |

#### Embedding (optional)

| Variable | Default | Description |
|----------|---------|-------------|
| `AWARENESS_EMBEDDING_PROVIDER` | _(none)_ | Set to `ollama` to enable semantic search. Without it, all features work except `semantic_search` and `backfill_embeddings`. |
| `AWARENESS_EMBEDDING_MODEL` | `nomic-embed-text` | Ollama model name for embeddings. Must match the model pulled in the Ollama container. |
| `AWARENESS_OLLAMA_URL` | `http://ollama:11434` | Ollama API endpoint. Default works with Docker Compose; change for external Ollama instances. |
| `AWARENESS_EMBEDDING_DIMENSIONS` | `768` | Vector dimensions. Must match the model output. Only change if using a non-default model. |

#### Docker Compose

| Variable | Default | Description |
|----------|---------|-------------|
| `POSTGRES_PASSWORD` | `awareness-dev` | Postgres password. Change for any non-demo deployment. |
| `AWARENESS_PG_DATA` | `~/awareness-pg` | Host path for Postgres data volume. |
| `AWARENESS_OLLAMA_DATA` | `~/awareness-ollama` | Host path for Ollama model cache volume. |
| `CLOUDFLARED_CONFIG` | `~/.cloudflared` | Path to cloudflared config directory (named tunnel). |
| `CLOUDFLARED_CREDS` | _(required for named tunnel)_ | Path to tunnel credentials JSON file. |

### Development

```bash
pip install -e ".[dev]"    # install with dev dependencies
python -m pytest tests/    # run tests
ruff check src/ tests/     # lint
mypy src/mcp_awareness/    # type check
```

## Tools

The server exposes 29 MCP tools. Clients that support MCP resources also get 6 read-only resources, but since not all clients surface resources, every resource has a tool mirror.

### Read tools

| Tool | Description |
|------|-------------|
| `get_briefing` | Compact awareness summary (~200 tokens all-clear, ~500 with issues). Call at conversation start. Pre-filtered through patterns and suppressions. |
| `get_alerts` | Active alerts, optionally filtered by source. Drill-down from briefing. |
| `get_status` | Full status for a specific source including metrics and inventory. |
| `get_knowledge` | Knowledge entries (patterns, context, preferences, notes). Filter by source, tags, entry_type, since/until, created_after/created_before, learned_from. `hint` param re-ranks results by semantic similarity. |
| `get_suppressions` | Active alert suppressions with expiry times and escalation settings. |
| `get_stats` | Entry counts by type, list of sources, total count. Call before `get_knowledge` to decide whether to filter. |
| `get_tags` | All tags in use with usage counts. Use to discover existing tags and prevent drift. |
| `get_intentions` | Pending/active intentions, optionally filtered by state, source, or tags. |
| `get_reads` | Read history for entries — who read what, when, from which platform. |
| `get_actions` | Action history — what was done because of an entry. |
| `get_unread` | Entries with zero reads. Cleanup candidates or missed knowledge. |
| `get_activity` | Combined read + action feed, chronological. |
| `get_related` | Bidirectional entry relationships — entries referenced via `related_ids` and entries that reference the given entry. |
| `semantic_search` | Find entries by meaning using vector similarity (pgvector + Ollama). Combines with tag/source/type/date filters. Requires embedding provider. |

### Write tools

| Tool | Description |
|------|-------------|
| `report_status` | Report system status. Called periodically by edge processes. Upserts one entry per source; stale if TTL expires without refresh. |
| `report_alert` | Report or resolve an alert. Captures diagnostics at detection time. Levels: `warning`, `critical`. Types: `threshold`, `structural`, `baseline`. |
| `remember` | Store permanent knowledge — facts still true in 30 days. Optional `content` payload with MIME `content_type`. Default choice for personal facts, project notes, design decisions. |
| `add_context` | Store temporal knowledge that auto-expires (default 30 days). Use for current events, milestones, or temporary states that will become stale. |
| `learn_pattern` | Record an if/then operational rule with conditions/effects for alert matching. Use only when there's a clear "when X happens, expect Y" relationship. |
| `set_preference` | Set a portable presentation preference (e.g., `alert_verbosity`, `check_frequency`). Upserts by key + scope. |
| `suppress_alert` | Suppress alerts by source/tags/metric. Time-limited with escalation override — critical alerts can break through. |
| `remind` | Create a todo, reminder, or planned action. Optional `deliver_at` timestamp for time-based surfacing. Intentions have a lifecycle: pending → fired → active → completed. |
| `update_intention` | Transition an intention state: pending → fired → active → completed/snoozed/cancelled. |
| `acted_on` | Log that you took action because of an entry. Tags inherited from the entry. |

### Data management tools

| Tool | Description |
|------|-------------|
| `update_entry` | Update a knowledge entry in place (note, pattern, context, preference). Tracks changes in `changelog`. Status/alert/suppression are immutable. |
| `delete_entry` | Soft-delete entries (30-day trash). By ID, by source + type, or by source. Bulk deletes require `confirm=True` (dry-run by default). |
| `restore_entry` | Restore a soft-deleted entry from trash. |
| `get_deleted` | List all entries in trash with IDs for restore. |
| `backfill_embeddings` | Generate embeddings for entries that don't have one yet, and re-embed stale entries whose content changed. Requires embedding provider. |

See the [Data Dictionary](docs/data-dictionary.md) for full schema documentation.

## Security

The awareness store may contain personal information. Securing the endpoint is not optional. The current approach uses two layers:

1. **Cloudflare WAF** — blocks requests at the edge if the URL path doesn't match the secret prefix. Unauthorized traffic never reaches your machine.
2. **Server middleware** — strips the secret prefix and routes to `/mcp`. Requests without it get 404.

See [Security considerations](docs/deployment-guide.md#security-considerations) in the Deployment Guide for details, limitations, and what's planned.

## Current status

**Working end-to-end** — deployed on `mcpawareness.com` via Cloudflare Tunnel with WAF protection. Tested with Claude (all platforms), Cursor, and VS Code.

### Getting started
- **One-line demo install** — `curl | bash` sets up Awareness + Postgres + Cloudflare quick tunnel with pre-loaded demo data and a `getting-started` prompt that personalizes your instance
- **Published Docker images** — `ghcr.io/cmeans/mcp-awareness` (GHCR) and Docker Hub, auto-built on release tags
- **Optional semantic search** — add `AWARENESS_EMBEDDING_PROVIDER=ollama` and `docker compose --profile embeddings up -d` for vector similarity search

### Knowledge store
- `remember`, `learn_pattern`, `add_context`, `set_preference` with filtered retrieval
- Idempotent upserts via `logical_key` — same source + key updates in place with changelog tracking
- In-place updates with changelog tracking (`update_entry` + `include_history`)
- General-purpose notes with optional content payload and MIME type
- Store introspection: `get_stats` for entry counts, `get_tags` for tag discovery
- Soft delete with 30-day trash, dry-run confirmation for bulk operations
- Delete and restore by tags with AND logic
- Pagination (`limit`/`offset`) on all list queries
- Entry relationships via `related_ids` convention + `get_related` bidirectional traversal

### Semantic search
- `semantic_search` tool — find entries by meaning using pgvector cosine similarity
- `backfill_embeddings` tool — embed pre-existing entries and re-embed stale ones
- `hint` parameter on `get_knowledge` — re-rank tag-filtered results by semantic similarity
- Background embedding generation via thread pool (non-blocking writes)
- Stale embedding detection via `text_hash` comparison
- Powered by Ollama (`nomic-embed-text`, 768 dimensions) — optional, self-hosted, zero cost
- Graceful degradation: everything works without an embedding provider

### Awareness engine
- Ambient awareness: status reporting, alert detection, suppression, briefing generation
- Three-layer detection model (threshold + knowledge implemented; baseline planned)
- Suppression system with time-based expiry and escalation overrides
- Intentions with time-based triggers and lifecycle (pending → fired → active → completed/snoozed/cancelled)
- Read/action tracking for audit and activity feeds

### MCP interface
- Full MCP API: 6 resources + 29 tools + 5 prompts
- Read tool mirrors for tools-only clients
- User-defined custom prompts from store entries with `{{var}}` templates
- Streamable HTTP + stdio transports

### Infrastructure
- PostgreSQL 17 with pgvector, GIN-indexed tag queries, HNSW-indexed embeddings, Debezium CDC-ready
- Connection pooling (psycopg_pool, min 2 / max 5) with automatic health checks and reconnection
- List mode and since/until/created_after/created_before filters for lightweight queries
- Storage abstraction: `Store` protocol — backends are swappable without changing server or collator logic
- Alembic migration framework (version-tracked, raw SQL, auto-runs on Docker startup)
- Secret path auth + Cloudflare WAF for edge-level access control
- Docker Compose with Postgres, optional Ollama, named Cloudflare Tunnel, or ephemeral quick tunnel
- Request timing instrumentation and `/health` endpoint
- 346 tests (all against real Postgres + Ollama in CI), strict type checking, CI pipeline with coverage, QA gate

### Not yet implemented
- Layer 2 (baseline) detection — rolling averages and deviation calculation
- OAuth / API key authentication — current auth is secret-path-based

### Planned edge providers

Edge providers are lightweight processes that monitor external systems and write knowledge into awareness. Each is simple alone; the server correlates them into well-timed, actionable intelligence. No automated producers yet ([example script](examples/simulate_edge.py) demonstrates the write path).

| Provider | Source | What it writes |
|----------|--------|----------------|
| **Google Calendar** | GCal API (polling) | Today's events as `add_context` entries. Any agent knows your schedule without direct calendar access. |
| **GPS / Location** | Phone (Tasker/Shortcuts) | Current coordinates. Triggers location-based intentions, corroborates meeting attendance, auto-completes travel intentions. |
| **NAS / Infrastructure** | Synology, Proxmox | Disk health, container status, backup state. The original monitoring use case. |
| **Health / Wearable** | Garmin, Apple Health | Sleep, HRV, activity. Cross-domain correlations (sleep quality vs. productivity). |
| **Home Assistant / Vision** | HA + cameras (RPi, GoPro) | Household state detection — trash accumulation, packages at door, laundry pile. Paired with calendar: "trash building up + leaving by car → take it down, leave 5 min early." |

**Multi-edge correlation** is where the real value lives. No single edge makes decisions alone — calendar provides the *expectation*, GPS provides the *evidence*, vision provides the *trigger*, and the server reconciles them into intentions that self-resolve through the lifecycle: pending → active → completed, with minimal user intervention.

## Vision

Every app you use knows one thing about you. Your calendar knows your schedule. Your health tracker knows your sleep. Your NAS knows your disk usage. None of them know each other.

Awareness fills that gap — a self-hosted store where knowledge from disconnected contexts accumulates, and agents surface the connections no single app can see.

**The product is silence.** The most important briefing is `attention_needed: false` — confirmation that everything was checked and nothing needs you. An attention firewall, not another notification source.

**Knowledge becomes ambient.** It accumulates through daily use, not documentation. A living estate document that's always current because you and your agents maintain it together as a natural part of working. A house that remembers when the furnace was serviced. A decision trail that preserves the reasoning at the moment you made the choice.

**Goals, not reminders.** Intentions are already a decision-support system. "Pick up milk" isn't a notification — it's a goal that your agent evaluates against real-world circumstances. As signal sources come online (GPS, calendar, vision), intentions will fire based on location, time, and context — with your agent surfacing recommendations and you deciding what to act on.

**Personal → family → team → community.** One person today. A shared household store next. Team knowledge that accumulates through work. Community institutional memory for organizations with zero software budget.

Read the full vision: **[What Knowledge Becomes When It's Ambient](docs/vision.md)**

## Design docs

- [Case Studies](docs/case-studies.md) — real-world examples of awareness in practice, with agent attribution
- [Vision](docs/vision.md) — what knowledge becomes when it's ambient: silence, estate planning, place memory, intentions, and the progression from personal to community
- [Deployment Guide](docs/deployment-guide.md) — demo install, secure deployment with Cloudflare Tunnel + WAF, client configuration
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

This project is built using the thing it builds. The developer works across multiple AI platforms — Claude Desktop, Claude Code, Claude on Android — and the friction encountered in daily use drives the features that get built. The agents are tools; the human directs every decision.

Claude Desktop surfaced a data pollution problem; the user directed the fix through Claude Code. A plan drafted on mobile during a commute was waiting in awareness when the user sat down at the desk. Claude Code flagged aspirational README claims; the user confirmed and corrected them.

Each interaction generated a case study. Read them all: **[Case Studies — Awareness in Practice](docs/case-studies.md)**

## License

Apache 2.0 — see [LICENSE](LICENSE) for details.

---

*Part of the Awareness ecosystem. Copyright (c) 2026 Chris Means.*
