# mcp-awareness

> **Every system tells you what's happening. None of them tell you why.**

> [!NOTE]
> This project is a **proof of concept**. The architecture is designed, the core is implemented and tested, but it has not yet been deployed against real systems. Expect rough edges.

## Background

This project started with a single memory instruction in Claude.ai:

> *"On the first turn of each conversation, call `synology-admin:get_resource_usage`. If CPU > 90%, RAM > 85%, any disk > 90% busy, or network/disk I/O looks abnormally high, briefly mention it as an FYI before responding."*

That worked surprisingly well. Infrastructure awareness surfaced inline during unrelated conversations. The agent applied contextual judgment — it knew the NAS was a seedbox, so it didn't flag normal seeding activity. Conversational tuning worked too: "don't bug me until it's 97%" adjusted behavior immediately.

But it had obvious limits. Diagnostics weren't captured at detection time (by the time you asked "why is CPU high?", the spike might be over). There was no structural detection — if a key process stopped, every metric looked *better*, and nothing alerted. Knowledge lived in platform-locked memory. It only worked with one system, on one platform.

The [original LinkedIn post](https://www.linkedin.com/posts/cmeans_mcp-modelcontextprotocol-platformengineering-activity-7440439710315098112-Fstj) tells the full story of how this pattern emerged.

`mcp-awareness` is the generalization of that experiment. The goals:

- **Multi-source awareness** — any number of edge processes (NAS daemons, calendar processors, CI/CD watchers) write to one store
- **Structural detection** — catch when the *shape* of a system changes, not just when a number crosses a line
- **Externalized knowledge** — operational patterns learned through conversation belong to the *system*, not to any single agent or platform
- **Token efficiency** — a pre-computed briefing (~200 tokens) so agents don't burn context discovering "nothing is wrong"
- **Platform portability** — any MCP client reads the same store (Claude.ai, Claude Code, Cursor, future LLMs)

## Three-layer detection

| Layer | Question | Catches |
|-------|----------|---------|
| **Threshold** | "Is this number too high?" | CPU > 90%, disk > 95% full |
| **Baseline** | "Is this abnormal for THIS system?" | Deviation from rolling average |
| **Knowledge** | "Does this match what I expect?" | Process stopped, replication stalled, SKU absent from orders |

The third layer is where the value is. Knowledge accumulates through conversation, not YAML.

## Architecture

```mermaid
flowchart TB
    subgraph Edge["Edge Processes"]
        E1["NAS Health Daemon"]
        E2["Calendar Processor"]
        E3["CI/CD Watcher"]
    end

    subgraph Server["mcp-awareness"]
        direction LR
        Store["Raw Store\n(SQLite)"]
        Collator["Collator\n• applies patterns\n• applies suppressions\n• evaluates escalation"]
        Briefing["Briefing Cache\n~200 tokens"]
        Store --> Collator --> Briefing
    end

    subgraph Resources["Resources (read)"]
        R1["awareness://briefing"]
        R2["awareness://alerts"]
        R3["awareness://status/{source}"]
        R4["awareness://knowledge"]
        R5["awareness://suppressions"]
    end

    subgraph Tools["Tools (write)"]
        T1["report_status"]
        T2["report_alert"]
        T3["learn_pattern"]
        T4["suppress_alert"]
        T5["add_context"]
        T6["set_preference"]
    end

    Edge -- "writes" --> Tools
    Tools --> Store
    Briefing --> Resources
    Resources -- "MCP (stdio or HTTP)" --> Client["Any MCP Client\n(Claude.ai, Claude Code,\nCursor, ...)"]
```

The agent reads `awareness://briefing` at conversation start. If `attention_needed` is true, it mentions the issue. If not, silence. Drill-down resources are available when the user asks for details.

## Quick start

```bash
# Install
pip install -e .

# Run via stdio (for MCP client integration)
mcp-awareness

# Or with a custom data directory
AWARENESS_DATA_DIR=./my-data mcp-awareness
```

### Claude Desktop / Claude Code config

```json
{
  "mcpServers": {
    "mcp-awareness": {
      "command": "mcp-awareness"
    }
  }
}
```

### Docker

```bash
docker compose up -d
```

## Design docs

- [From Metrics to Mental Models](docs/from-metrics-to-mental-models.md) — core spec: three-layer detection model, API design, data schema, implementation priorities
- [Collation Layer](docs/collation-layer.md) — addendum: briefing resource, token optimization, escalation logic, backend placement

## Acknowledgements

This project was designed and built through a collaborative process between [Chris Means](https://github.com/cmeans) and [Claude](https://claude.ai) (Anthropic's AI assistant). The architecture emerged from real-world experience with a homelab seedbox — Chris identified the pattern gap (monitoring that can't see structural changes), and the design was developed iteratively through conversation. Claude contributed to the architecture documents, implemented the codebase, and wrote the tests. Every design decision was discussed and validated by Chris before being committed.

The collaboration model itself is part of what this project explores: AI agents that build up operational knowledge through conversation rather than configuration. The awareness service is, in a sense, a formalization of how that collaboration already works — just extended to infrastructure.

## License

MIT — see [LICENSE](LICENSE) for details.

---

Copyright (c) 2026 Chris Means
