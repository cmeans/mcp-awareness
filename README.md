# mcp-awareness

**Every system tells you what's happening. None of them tell you why.**

Your monitoring stack fires when CPU hits 90%. But on a seedbox that runs hot by design, 90% CPU is Tuesday. Meanwhile, a key process silently stops — CPU drops, disk goes quiet, every metric looks *better than usual* — and nothing alerts because nothing crossed a threshold. The system is broken, and your monitoring agrees everything is fine.

`mcp-awareness` is a generic [MCP](https://modelcontextprotocol.io/) server that gives AI agents ambient awareness across monitored systems. Edge processes write tagged status, alerts, and knowledge. Any MCP client reads a unified, token-optimized briefing. The agent learns what "normal" looks like through conversation and acts on deviations — not just threshold breaches, but structural changes that metrics-only monitoring misses.

## Three-layer detection

| Layer | Question | Catches |
|-------|----------|---------|
| **Threshold** | "Is this number too high?" | CPU > 90%, disk > 95% full |
| **Baseline** | "Is this abnormal for THIS system?" | Deviation from rolling average |
| **Knowledge** | "Does this match what I expect?" | Process stopped, replication stalled, SKU absent from orders |

The third layer is where the value is. Knowledge accumulates through conversation, not YAML.

## Architecture

```
Edge Processes (NAS daemon, calendar processor, CI/CD watcher, ...)
    │
    │  writes (report_status, report_alert, learn_pattern, ...)
    ▼
┌──────────────────────────────────────────────────────────────┐
│                       mcp-awareness                          │
│                                                              │
│  ┌─────────────┐    ┌──────────────┐    ┌────────────────┐  │
│  │  Raw Store   │───▶│  Collator    │───▶│  Briefing      │  │
│  │  (tagged     │    │  (applies    │    │  Cache         │  │
│  │   entries)   │    │   patterns,  │    │  (~200 tokens) │  │
│  │              │    │   suppress.) │    │                │  │
│  └─────────────┘    └──────────────┘    └────────────────┘  │
│                                                              │
│  Resources (read):           Tools (write):                  │
│  • awareness://briefing      • report_status                 │
│  • awareness://alerts        • report_alert                  │
│  • awareness://status/{src}  • learn_pattern                 │
│  • awareness://knowledge     • suppress_alert                │
│  • awareness://suppressions  • add_context / set_preference  │
└──────────────────────────────────────────────────────────────┘
    │
    │  MCP (stdio or Streamable HTTP)
    ▼
Any MCP Client (Claude.ai, Claude Code, Cursor, ...)
```

The agent reads `awareness://briefing` at conversation start (~200 tokens). If `attention_needed` is true, it mentions the issue. If not, silence. Drill-down resources are available if the user asks for details.

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

## Design docs

- [From Metrics to Mental Models](docs/from-metrics-to-mental-models.md) — core spec, API design, data schema
- [Collation Layer](docs/collation-layer.md) — briefing resource, token optimization, escalation logic

## License

MIT
