# Architecture Addendum: Collation Layer & Token Optimization

Supplement to `from-metrics-to-mental-models.md`. These additions should be integrated into the main spec.

---

## The Token Problem

As source providers multiply and the knowledge store grows, the raw data the agent would need to read on every conversation start becomes prohibitively large:

- 5 source providers × ~500 tokens each = 2,500 tokens of status
- Active alerts with embedded diagnostics = 500-2,000 tokens per alert
- Knowledge store (patterns, suppressions, context) = grows without bound
- Historical context = grows over time

Reading all of this on every turn is wasteful. Most of the time, the answer is "nothing needs your attention." The agent shouldn't burn tokens discovering that.

## Solution: The Collation Layer

Add a background process **inside the awareness service** that continuously digests the raw store into a compact, agent-optimized briefing.

### Updated Architecture

```
Edge Processes (NAS daemon, calendar processor, CI/CD watcher, etc.)
    │
    │  writes (report_status, report_alert, learn_pattern, etc.)
    ▼
┌──────────────────────────────────────────────────────────────┐
│                       mcp-awareness                          │
│                                                              │
│  ┌─────────────┐    ┌──────────────┐    ┌────────────────┐  │
│  │  Raw Store   │───▶│  Collator    │───▶│  Briefing      │  │
│  │              │    │  (background) │    │  Cache         │  │
│  │  • status    │    │              │    │                │  │
│  │  • alerts    │    │  • Scans raw │    │  ~200 tokens   │  │
│  │  • knowledge │    │    store     │    │  Pre-digested  │  │
│  │  • inventory │    │  • Applies   │    │  Always fresh  │  │
│  │  • history   │    │    patterns  │    │                │  │
│  │  • suppress. │    │  • Evaluates │    │  awareness://  │  │
│  │              │    │    suppress. │    │    briefing    │  │
│  │  Full data   │    │  • Generates │    │                │  │
│  │  (canonical) │    │    summary  │    │  Rebuilt on    │  │
│  │              │    │              │    │  every raw     │  │
│  │              │    │              │    │  store change  │  │
│  └─────────────┘    └──────────────┘    └────────────────┘  │
│                                                              │
│  Resources:                Tools:                            │
│  • awareness://briefing    • report_status                   │
│    (agent reads this)      • report_alert                    │
│  • awareness://alerts      • learn_pattern                   │
│    (drill-down)            • suppress_alert                  │
│  • awareness://status/*    • add_context                     │
│    (drill-down)            • set_preference                  │
│  • awareness://knowledge   • clear_suppression               │
│    (drill-down)                                              │
└──────────────────────────────────────────────────────────────┘
```

### The Briefing Resource

`awareness://briefing` is the only resource the agent reads on every conversation start. It's a pre-computed, compact summary designed to minimize token usage while conveying everything the agent needs to decide whether to speak up.

**Design constraints:**
- Target: under 200 tokens when nothing is wrong
- Under 500 tokens when there are active issues
- Never includes raw metrics — only conclusions
- References drill-down resources for details the agent can fetch if needed

**Example briefing — all clear:**

```json
{
  "generated": "2026-03-19T15:00:00Z",
  "staleness_sec": 12,
  "summary": "All clear across 3 sources.",
  "sources": {
    "synology-nas": { "status": "ok", "last_report": "2026-03-19T14:59:48Z" },
    "gcal": { "status": "ok", "last_report": "2026-03-19T14:58:00Z" },
    "github-ci": { "status": "ok", "last_report": "2026-03-19T14:55:00Z" }
  },
  "active_alerts": 0,
  "active_suppressions": 1,
  "upcoming": [],
  "attention_needed": false
}
```

Agent reads this, sees `attention_needed: false`, doesn't mention anything. Total cost: ~80 tokens.

**Example briefing — issues present:**

```json
{
  "generated": "2026-03-19T15:00:00Z",
  "staleness_sec": 5,
  "summary": "1 warning on synology-nas. Calendar item in 40 min with unresolved context.",
  "sources": {
    "synology-nas": {
      "status": "warning",
      "last_report": "2026-03-19T14:59:55Z",
      "headline": "qBittorrent stopped — should always be running",
      "drill_down": "awareness://alerts/synology-nas"
    },
    "gcal": {
      "status": "info",
      "last_report": "2026-03-19T14:58:00Z",
      "headline": "Q3 planning with Sarah in 40 min — 3 unresolved items from last thread",
      "drill_down": "awareness://status/gcal"
    },
    "github-ci": { "status": "ok", "last_report": "2026-03-19T14:55:00Z" }
  },
  "active_alerts": 1,
  "active_suppressions": 0,
  "upcoming": [
    { "source": "gcal", "summary": "Q3 planning — leave in 25 min (traffic +15 min)" }
  ],
  "attention_needed": true,
  "suggested_mention": "FYI: qBittorrent is down on the NAS (should always be running). Also, Q3 planning with Sarah in 40 minutes — there are 3 unresolved questions from last week's thread and traffic will add about 15 minutes to your commute."
}
```

Agent reads this, sees `attention_needed: true`, uses `suggested_mention` or composes its own from the headlines. If the user asks for details, agent drills into the referenced resources. Total cost: ~250 tokens.

**Key field: `suggested_mention`**

When attention is needed, the collator generates a pre-composed mention that the agent can use directly or rephrase. This further reduces the agent's work — it doesn't need to synthesize across sources, the collator already did that. The agent just needs to decide whether and how to deliver it.

### Collation Logic

The collator runs inside the awareness service as a background task. It rebuilds the briefing whenever the raw store changes. The logic:

```python
def generate_briefing(store: AwarenessStore) -> dict:
    briefing = {
        "generated": now_utc(),
        "sources": {},
        "active_alerts": 0,
        "active_suppressions": 0,
        "upcoming": [],
        "attention_needed": False,
    }

    for source in store.get_sources():
        status = store.get_latest_status(source)
        alerts = store.get_active_alerts(source)
        suppressions = store.get_active_suppressions(source)

        # Check for stale sources (TTL expired)
        if status and status.is_stale():
            briefing["sources"][source] = {
                "status": "stale",
                "headline": f"{source} has not reported in {status.age_sec}s",
                "drill_down": f"awareness://status/{source}"
            }
            briefing["attention_needed"] = True
            continue

        # Apply suppressions — filter out suppressed alerts
        active_alerts = [a for a in alerts if not is_suppressed(a, suppressions)]

        # Apply learned patterns — filter out expected anomalies
        patterns = store.get_patterns(source)
        active_alerts = [a for a in active_alerts if not matches_pattern(a, patterns)]

        # Determine source status
        if any(a.level == "critical" for a in active_alerts):
            source_status = "critical"
        elif active_alerts:
            source_status = "warning"
        else:
            source_status = "ok"

        source_entry = {
            "status": source_status,
            "last_report": status.timestamp,
        }

        if active_alerts:
            # Use the most severe alert's message as the headline
            top_alert = max(active_alerts, key=lambda a: severity_rank(a.level))
            source_entry["headline"] = top_alert.message
            source_entry["drill_down"] = f"awareness://alerts/{source}"
            briefing["active_alerts"] += len(active_alerts)
            briefing["attention_needed"] = True

        briefing["sources"][source] = source_entry

    # Process upcoming items (calendar, scheduled tasks, etc.)
    upcoming = store.get_upcoming_items()
    briefing["upcoming"] = [
        {"source": item.source, "summary": item.summary}
        for item in upcoming
    ]
    if upcoming:
        briefing["attention_needed"] = True

    # Count active suppressions
    briefing["active_suppressions"] = store.count_active_suppressions()

    # Generate summary line
    briefing["summary"] = compose_summary(briefing)

    # Generate suggested mention if attention needed
    if briefing["attention_needed"]:
        briefing["suggested_mention"] = compose_mention(briefing)

    return briefing
```

### Pattern and Suppression Application

The collator — not the agent — applies patterns and suppressions. This is important:

- Learned pattern says "qBittorrent stops on Fridays for maintenance" + today is Friday + qBittorrent is stopped → **the collator filters this out before the agent ever sees it**
- Active suppression says "ignore disk_busy_pct warnings until 4 PM" → **the collator filters it**
- Suppression expired → collator stops filtering, alert reappears in briefing

This means the agent doesn't need to read patterns and suppressions separately and apply its own logic. The briefing is pre-filtered. The raw data is still available for drill-down if the agent or user wants to inspect it.

### Escalation in the Collator

Suppressions with `escalation_override: true` are re-evaluated by the collator:

```python
def is_suppressed(alert, suppressions) -> bool:
    for s in suppressions:
        if s.matches(alert) and not s.is_expired():
            if s.escalation_override:
                # Check if conditions warrant breaking through
                if alert.level_exceeds(s.suppress_level):
                    return False  # Escalated — don't suppress
                if alert.has_worsened_significantly(s.original_value):
                    return False  # Worsened — don't suppress
            return True  # Suppressed
    return False  # No matching suppression
```

---

## Backend Placement

### Where should `mcp-awareness` run?

The awareness service needs to be reachable by edge processes (writing) and MCP clients (reading). It should also survive the failure of any individual monitored system.

| Location | Pros | Cons |
|----------|------|------|
| **On the NAS** | Always on, Docker-ready, co-located with primary edge source | If NAS goes down, awareness goes down — at the moment you need it most |
| **On Proxmox (VM or LXC)** | Survives NAS failure, more resources, right layer for infra services | Separate management, needs network access to NAS |
| **Cloud (fly.io, VPS)** | Survives all local failures, accessible from anywhere | Latency, cost, edge processes need outbound access |
| **Local (developer machine)** | Simplest for PoC, no deployment | Dies when laptop sleeps, not always-on |

### Recommendation

**PoC:** Local (developer machine via stdio). Proves the concept without deployment complexity.

**Phase 1:** Proxmox (LXC container). The awareness service is an infrastructure concern — it belongs on the infrastructure layer, not on the thing being monitored. Lightweight LXC container, minimal resources (64MB RAM, 0.1 CPU). Edge processes on the NAS connect via HTTP.

**Phase 2+:** Consider cloud if remote access becomes important (e.g., monitoring while traveling). Cloudflare Tunnel or similar for secure exposure without opening ports.

### Network Topology (Phase 1)

```
┌──────────────────────────────────┐
│  Proxmox Host                    │
│                                  │
│  ┌────────────────────────┐      │
│  │  LXC: mcp-awareness    │      │
│  │  port 8420              │◄─────── MCP clients (stdio or HTTP)
│  │  SQLite store           │      │
│  └────────────┬───────────┘      │
│               │                  │
└───────────────┼──────────────────┘
                │ HTTP (internal network)
┌───────────────┼──────────────────┐
│  Synology NAS │                  │
│               │                  │
│  ┌────────────▼───────────┐      │
│  │  homelab-edge daemon    │      │
│  │  (Docker container)     │      │
│  └────────────────────────┘      │
└──────────────────────────────────┘
```

---

## Updated Resource Hierarchy

With the collation layer, the resource hierarchy becomes:

```
awareness://briefing              ← Agent reads THIS on every turn (~200 tokens)
    │
    ├── awareness://alerts            ← Drill-down: all active alerts (if briefing says attention needed)
    │   └── awareness://alerts/{src}  ← Drill-down: alerts from specific source
    │
    ├── awareness://status            ← Drill-down: full status all sources
    │   └── awareness://status/{src}  ← Drill-down: specific source status + metrics + inventory
    │
    ├── awareness://knowledge         ← Drill-down: patterns, context
    │   └── awareness://knowledge?tags=X  ← Filtered by tag
    │
    ├── awareness://suppressions      ← Drill-down: active suppressions
    │
    └── awareness://history           ← Drill-down: resolved alerts
```

**Agent instruction becomes:**
> "At conversation start, read `awareness://briefing`. If `attention_needed` is true, mention the `suggested_mention` or compose your own from the source headlines. If the user asks for details, drill into the referenced resources. Don't read anything else unless asked or unless the briefing indicates an issue."

This is dramatically simpler than "read alerts, read knowledge, read suppressions, apply patterns, evaluate escalations, compose a summary." The collator does all that work once, the agent reads the result.

---

## Updated Priority Table (Additions)

| Priority | Task | Effort | Value |
|----------|------|--------|-------|
| P0 | Collation logic + briefing generation | Medium | Token optimization |
| P0 | `awareness://briefing` resource | Low | Primary agent interface |
| P0 | `suggested_mention` composition | Low | Further reduces agent work |
| P1 | Pattern application in collator (not agent) | Medium | Correct suppression behavior |
| P1 | Suppression escalation evaluation in collator | Medium | Escalation override |
| P1 | Stale source detection (TTL expiry) | Low | Reliability |
| P2 | Proxmox LXC deployment | Low | Production backend |
| P2 | SQLite backend with WAL mode | Medium | Scale + concurrent access |

---

## Updated Open Questions

8. **Should edge processes be MCP clients or use a simpler REST API?** The edge daemon needs to call `report_status` and `report_alert` on the awareness service. Making it a full MCP client adds complexity (session management, capability negotiation). A simple REST endpoint alongside the MCP server might be more practical for edge → service communication, while MCP is used for agent → service communication.

9. **Briefing staleness**: The briefing is rebuilt on every raw store change. But if no sources report for a while (all sources healthy, nothing to say), the briefing could be stale. Should the collator add a heartbeat timestamp so the agent knows the briefing is current? (Added `staleness_sec` to the schema for this.)

10. **Who composes the `suggested_mention`?** The collator generates it, but it's a natural-language string. Should it be a template with variable substitution (deterministic, boring), or should it use a lightweight LLM call to compose something conversational? The template approach is simpler and cheaper. The LLM approach is more natural but adds a dependency and latency to the collation loop.
