# From Metrics to Mental Models: Ambient System Awareness via MCP

> **Note:** This document was written during initial design (March 2026) and describes the architectural thinking behind the project. Many details have evolved — the storage backend is PostgreSQL-only (v0.6.0+), semantic search via pgvector + Ollama is implemented (v0.10.0+), intentions have a full lifecycle (v0.8.0+), and most P0–P2 items in the priority table have shipped. See the [CHANGELOG](../CHANGELOG.md) for current state and the [vision document](vision.md) for where the project is heading.

## What This Is

A design pattern for AI agents that don't just query systems — they understand them. The pattern combines edge intelligence (lightweight processes that compute contextual awareness), the Model Context Protocol (MCP) as the data transport, and an externalized knowledge store as the durable memory layer. The result is an agent that knows what "normal" looks like across multiple systems, notices when reality diverges from that model, and surfaces it conversationally with enough judgment to know when to speak up.

This is not a single-system monitoring tool. It's a **generic awareness service** — `mcp-awareness` — that any number of edge processes can write to and any MCP client can read from. A NAS health daemon, a calendar pre-processor, a CI/CD pipeline watcher, and a database replication monitor all feed the same store, tagged by source and domain. The agent reads one unified view.

### What This Is Not

- **Not a replacement for your monitoring stack.** Prometheus, Datadog, Netdata, Grafana — those are good at what they do. This sits on top as an interpretation layer.
- **Not an AI-powered dashboard.** There is no dashboard. Awareness surfaces inline during normal conversations — proactively, not on demand.
- **Not a rules engine.** A rules engine can catch structural anomalies if someone writes the rules. The insight here is that knowledge accumulates through conversation, not YAML.
- **Not platform-locked.** The knowledge belongs to the *system*, not to any particular agent or chat platform. Any MCP client reads the same store.

---

## The Three-Layer Detection Model

The core conceptual framework. Each layer asks a fundamentally different question.

### Layer 1: Threshold-based — "Is this number too high?"

Simple metric comparison against static values. CPU > 90%, disk > 95% full, response time > 500ms. Every monitoring system starts here. Necessary, but insufficient.

**Failure mode:** On a system that runs hot by design (a seedbox, a build server, a high-throughput database), static thresholds produce constant false positives.

### Layer 2: Baseline-aware — "Is this abnormal for THIS system?"

Compare current metrics against a rolling baseline — the 24h average, the typical range for this time of day. Alerts fire on deviation from baseline rather than absolute values.

**Failure mode:** Baselines are still metric-centric. They catch deviations in numbers but are blind to structural changes. If a key process stops, CPU drops, disk goes quiet. Every metric looks healthy. The system looks *better than usual*. It's broken, and Layer 2 agrees everything is fine.

### Layer 3: Knowledge-based — "Does this look like what I expect?"

Compare the observed state against a mental model of what "normal" looks like — not just metrics, but structure: which services should be running, what traffic patterns are expected, what the shape of the system is.

Anomalies are detected not when a number crosses a line, but when the picture doesn't match the model.

**Cross-domain examples:**

| Domain | Layer 1 sees | Layer 2 sees | Layer 3 catches |
|--------|-------------|-------------|-----------------|
| Infrastructure | CPU > 90% alert | CPU normal, disk normal | qBittorrent stopped — should always be running. Metrics green because work stopped. |
| E-commerce | Order volume normal | Within seasonal range | Top SKU hasn't sold in 6 hours. Nothing went up. It went absent. |
| Calendar | Meeting at 2 PM | Normal for this day | Unresolved questions from last week's email thread + traffic adds 15 min today. |
| Database | Replication lag 200ms | Under threshold | Lag exactly 200ms for 4 hours. Normally fluctuates. Stalled, not slow. |

### Where the Knowledge Comes From

The agent's model of "normal" doesn't come from a config file. It comes from two sources:

1. **Expected state manifests** — deterministic, version-controlled definitions of what "normal" looks like. Per-source. (qBittorrent must be running. The top SKU should sell at least once per hour.)

2. **Accumulated conversational context** — the agent learns through interaction. "Chris sometimes stops qBittorrent for maintenance on Fridays." This knowledge is captured in the externalized knowledge store, not trapped in any single platform's memory system.

### The Rules Engine Counterargument

Could a well-configured rules engine catch Layer 3 anomalies? Yes — if someone enumerated every expected state in advance. But in practice, those rules rarely get written because the cost of formally specifying "what normal looks like" across an entire system is too high. The knowledge-based layer reduces that cost to zero — the model builds itself through interaction.

The agent is not more capable than a rules engine. It's more likely to actually have the relevant knowledge, because the cost of acquiring it is zero.

---

## Architecture: The Generic Awareness Service

### Core Insight: Separate the Store from the Sources

The previous design coupled the monitoring daemon, the MCP server, and the knowledge store into a single system-specific stack. The better architecture separates them:

```
┌─────────────────────┐  ┌─────────────────────┐  ┌─────────────────────┐
│  NAS Health Daemon   │  │  Calendar Processor  │  │  CI/CD Watcher      │
│  (edge process)      │  │  (edge process)      │  │  (edge process)     │
│  source: synology    │  │  source: gcal        │  │  source: github-ci  │
│  tags: infra, nas    │  │  tags: calendar, work│  │  tags: cicd, deploy │
└──────────┬──────────┘  └──────────┬──────────┘  └──────────┬──────────┘
           │                        │                        │
           │         writes (status, alerts, knowledge)      │
           ▼                        ▼                        ▼
    ┌──────────────────────────────────────────────────────────────┐
    │                      mcp-awareness                           │
    │                  (generic MCP server)                         │
    │                                                              │
    │  Resources (read):           Tools (write):                  │
    │  • awareness://status        • report_status                 │
    │  • awareness://alerts        • report_alert                  │
    │  • awareness://knowledge     • learn_pattern                 │
    │  • awareness://inventory     • suppress_alert                │
    │  • awareness://history       • add_context                   │
    │                              • clear_suppression             │
    │  All filterable by:                                          │
    │  source, tags, level,        All tagged with:                │
    │  type, time range            source, tags, domain            │
    └──────────────────────┬───────────────────────────────────────┘
                           │
                           │ MCP (stdio or Streamable HTTP)
                           ▼
                   ┌───────────────┐
                   │  Any MCP      │
                   │  Client       │
                   │  (Claude.ai,  │
                   │   Claude Code,│
                   │   Cursor,     │
                   │   future LLMs)│
                   └───────────────┘
```

### Three-Tier Knowledge Model

```
Tier 1: Edge Config (per-source config.yaml, expected_state.yaml)
    → Static, version-controlled, human-maintained
    → Defines thresholds, expected state, collection intervals
    → Lives with the edge process, not in the awareness service

Tier 2: Externalized Knowledge Store (knowledge.json in mcp-awareness)
    → Dynamic, agent-writable, portable across platforms
    → Operational patterns, suppressions, historical context, preferences
    → Any agent can read and write. Knowledge belongs to the SYSTEM, not the agent.

Tier 3: Agent Memory (Claude memory, Cursor context, etc.)
    → Ephemeral, platform-specific, supplementary only
    → Presentation preferences ("I like one-sentence alerts")
    → Platform-specific tuning that doesn't need to be portable
```

The critical shift: substantive knowledge about systems lives in Tier 2, not Tier 3. Agent memory becomes a thin preference layer, not the knowledge store.

---

## The `mcp-awareness` Service

### Design Principles

- **Source-agnostic**: The service doesn't know or care what a NAS is, what a calendar is, or what a CI/CD pipeline does. It stores tagged entries and serves them.
- **Tag-based filtering**: Every entry has a `source` (who wrote it), `tags` (categories), and `type` (status, alert, pattern, suppression, context). Agents query by any combination.
- **Write via tools, read via resources**: Edge processes and agents write through MCP tools. Agents read through MCP resources. Both sides are MCP-native.
- **No inference**: The service stores and serves. It doesn't evaluate thresholds, run diagnostics, or make decisions. That's the edge process's job (for detection) and the agent's job (for interpretation).

### Resources (Read API)

All resources support query parameters for filtering by source, tags, level, and type. Agents that don't filter get everything.

| URI | Description | Subscribe? |
|-----|-------------|-----------|
| `awareness://status` | Current status from all sources | Yes |
| `awareness://status/{source}` | Status from a specific source | Yes |
| `awareness://alerts` | Active alerts across all sources | Yes |
| `awareness://alerts/{source}` | Active alerts from a specific source | Yes |
| `awareness://knowledge` | All knowledge entries (patterns, context, preferences) | No |
| `awareness://knowledge?tags=infra` | Knowledge filtered by tag | No |
| `awareness://inventory` | System inventory across all sources | Yes |
| `awareness://inventory/{source}` | Inventory from a specific source | Yes |
| `awareness://history` | Resolved alert history | No |
| `awareness://suppressions` | Active suppressions | No |

**Resource descriptions carry behavioral hints** (learned from clipboard-mcp development — instructions alone may not get sufficient model attention):

```python
@mcp.resource("awareness://alerts")
async def all_alerts() -> str:
    """Active alerts across all monitored systems. Empty = all clear.
    Check at conversation start. If non-empty, briefly inform user
    before responding to their question. One sentence for warnings,
    short paragraph for critical. Group by source if multiple."""
    ...
```

### Tools (Write API)

Edge processes and agents use these to write to the store. Every write is tagged.

#### `report_status`

Edge processes call this periodically to report their current state.

```python
@mcp.tool()
async def report_status(
    source: str,           # "synology-nas", "gcal", "github-ci"
    tags: list[str],       # ["infra", "nas", "seedbox"]
    metrics: dict,         # Source-specific metrics blob
    inventory: dict = None,# Optional system inventory
    ttl_sec: int = 120     # Status expires if not refreshed
) -> str:
    """Report current system status. Called periodically by edge processes.
    If TTL expires without refresh, the source is marked stale."""
    ...
```

#### `report_alert`

Edge processes call this when an alert fires or resolves.

```python
@mcp.tool()
async def report_alert(
    source: str,
    tags: list[str],
    alert_id: str,         # Unique within source
    level: str,            # "warning", "critical"
    alert_type: str,       # "threshold", "structural", "baseline"
    message: str,
    details: dict = None,  # Source-specific details
    diagnostics: dict = None,  # Captured at detection time
    resolved: bool = False # Set True to resolve an existing alert
) -> str:
    """Report an alert or resolve an existing one. Diagnostics should
    be captured at detection time — evidence may be transient."""
    ...
```

#### `learn_pattern`

Agents call this when they learn something from conversation.

```python
@mcp.tool()
async def learn_pattern(
    source: str,           # Which system this applies to
    tags: list[str],
    description: str,      # "qBittorrent sometimes stopped for maintenance on Fridays"
    conditions: dict = None,  # {"day_of_week": "friday"}
    effect: str = None,    # "suppress qbittorrent_stopped alerts"
    learned_from: str = "conversation"
) -> str:
    """Record an operational pattern learned from conversation.
    Any agent can write; any agent can read. Knowledge is portable."""
    ...
```

#### `suppress_alert`

Agents or users call this to suppress alerts with structured expiry.

```python
@mcp.tool()
async def suppress_alert(
    source: str = None,    # Specific source, or None for all
    tags: list[str] = None,# Filter by tags, or None for all
    metric: str = None,    # Specific metric, or None for all from source
    level: str = "warning",# Suppress this level and below
    duration_minutes: int = 60,
    escalation_override: bool = True,
    reason: str = ""
) -> str:
    """Suppress alerts. Structured, time-limited, with escalation override.
    Not a plain-text memory edit — survives across agent platforms."""
    ...
```

#### `add_context`

Agents or edge processes record historical context.

```python
@mcp.tool()
async def add_context(
    source: str,
    tags: list[str],
    description: str,      # "sdb was replaced, RAID rebuilt March 15"
    expires_days: int = 30
) -> str:
    """Record historical context that any agent should know about.
    Auto-expires after specified duration."""
    ...
```

#### `set_preference`

Agent-level preferences that are still portable.

```python
@mcp.tool()
async def set_preference(
    key: str,              # "alert_verbosity", "check_frequency"
    value: str,            # "one_sentence_warnings", "first_turn_only"
    scope: str = "global"  # "global" or source-specific
) -> str:
    """Set a presentation preference. Portable across agent platforms."""
    ...
```

### Data Schema

All entries in the store share a common envelope:

```json
{
  "id": "unique-entry-id",
  "type": "status | alert | pattern | suppression | context | preference",
  "source": "synology-nas",
  "tags": ["infra", "nas", "seedbox"],
  "created": "2026-03-19T14:00:00Z",
  "updated": "2026-03-19T14:32:00Z",
  "expires": "2026-03-19T16:30:00Z",
  "data": { ... }
}
```

**Status entry `data`:**
```json
{
  "metrics": { "cpu": { "usage_pct": 34 }, "memory": { "usage_pct": 71 } },
  "inventory": { "docker": { "running": 14, "stopped": 1, "unexpected_stopped": [] } },
  "ttl_sec": 120,
  "daemon_status": "running"
}
```

**Alert entry `data`:**
```json
{
  "alert_id": "cpu-warn-20260319T143200Z",
  "level": "warning",
  "alert_type": "threshold",
  "metric": "cpu_pct",
  "value": 96.2,
  "threshold": 80,
  "sustained_sec": 480,
  "message": "CPU at 96.2% for 8+ minutes",
  "diagnostics": {
    "captured_at": "2026-03-19T14:24:05Z",
    "top_processes_cpu": [ { "name": "qbittorrent", "cpu_pct": 74.3 } ],
    "docker_containers": [ { "name": "qbt-cleanup", "cpu_pct": 68.2 } ]
  },
  "resolved": false
}
```

**Pattern entry `data`:**
```json
{
  "description": "qBittorrent sometimes stopped for maintenance on Fridays",
  "conditions": { "day_of_week": "friday" },
  "effect": "suppress qbittorrent_stopped alerts",
  "learned_from": "conversation"
}
```

**Suppression entry `data`:**
```json
{
  "metric": "disk_busy_pct",
  "suppress_level": "warning",
  "escalation_override": true,
  "reason": "User: 'stop bugging me about disk I/O for an hour'"
}
```

### Storage Backend

For the PoC: a single JSON file (`awareness-store.json`), written atomically. For production: a lightweight embedded database (SQLite) with TTL-based expiration and indexed queries on source/tags/type.

### Server Instructions

```python
mcp = FastMCP(
    name="mcp-awareness",
    instructions=(
        "This server provides ambient awareness across monitored systems. "
        "At conversation start, read awareness://alerts. If non-empty, briefly "
        "inform the user before responding to their question. Group alerts by "
        "source if multiple systems have issues. One sentence for warnings, "
        "short paragraph for critical. Don't re-check unless asked. "
        "When you learn something about a system from conversation, use "
        "learn_pattern to record it. When the user asks to suppress alerts, "
        "use suppress_alert — not a memory edit."
    ),
)
```

---

## Conversational Ops Tuning

### Two-Tier Tuning Model

| Layer | What it controls | How it's changed | Persistence |
|-------|-----------------|------------------|-------------|
| Edge config (`config.yaml`) | Core thresholds, collection intervals, diagnostic depth, expected state | File edit + daemon restart | Permanent, version-controlled |
| Awareness store (via tools) | Suppressions, patterns, context, preferences | Conversational ("stop bugging me") | Durable, portable, structured |

Agent memory (Tier 3) is now optional — only for platform-specific presentation preferences that aren't worth externalizing.

### Suppression with Escalation

```
User: "Stop bugging me about disk I/O for an hour"
→ Agent calls suppress_alert(source="synology-nas", metric="disk_busy_pct",
                             duration_minutes=60, escalation_override=True,
                             reason="User request")
```

**Escalation overrides:**
- Alert level escalates (warning → critical)
- A new, different alert fires on the same subsystem
- The suppressed condition has significantly worsened
- A correlated failure occurs

### The Feedback Loop

1. Agent alerts: "qBittorrent isn't running"
2. User: "yeah, I stopped it for maintenance"
3. Agent calls `learn_pattern(source="synology-nas", description="qBittorrent sometimes stopped for maintenance on Fridays", conditions={"day_of_week": "friday"}, effect="suppress qbittorrent_stopped")`
4. Pattern is now in the store — any agent, any platform, reads it
5. Future Fridays: any agent sees qBittorrent stopped + reads the pattern + doesn't alert

---

## Proof of Concept: What's Running Today

Before any daemon or awareness service was built, a minimal PoC was implemented using a memory instruction and an existing MCP tool:

**Memory instruction (live in Claude.ai):**
> "On the first turn of each conversation, call `synology-admin:get_resource_usage`. If CPU > 90%, RAM > 85%, any disk > 90% busy, or network/disk I/O looks abnormally high, briefly mention it as an FYI before responding."

**What this proves:**
- The UX pattern works — infrastructure awareness surfaces inline during unrelated conversations
- The agent applies contextual judgment — knows the NAS is a seedbox, doesn't flag normal seeding
- Conversational tuning works — "don't bug me until it's 97%" adjusts behavior
- Cost is one MCP tool call per conversation, not continuous monitoring

**What this doesn't prove:**
- Diagnosis at detection time (tool returns current metrics, not historical context)
- Knowledge-based structural detection (no container inventory, no expected-state comparison)
- Multi-source awareness (single system only)
- Externalized knowledge (all context is in platform-locked memory)
- Portability (works in Claude.ai only — tied to memory system)

---

## Reference Implementation: Homelab Edge Provider

The NAS health daemon is one **source provider** for the awareness service.

### Target Environment

- **Synology DS1618+** (DSM 7.x) — seedbox
- **Proxmox host** — secondary target (future)
- **Docker containers** on the NAS
- **Python 3.10+**

### Seedbox Baseline Awareness

The DS1618+ runs hot by design:

- Disk I/O: 80-90% busy is routine
- Network upload: sustained multi-MB/s
- CPU: 30-50% from qBittorrent
- RAM: 70%+

**Implications:**
1. Baseline learning: alert on deviation from rolling 24h average
2. Process-aware thresholds: don't alert if top consumer is qBittorrent
3. Structural detection is the primary value: "disk is quiet because qBittorrent stopped" is the real alert

### Edge Daemon

Collects metrics via Synology DSM WebAPI and Docker Engine API. Evaluates thresholds and structural expectations. Captures diagnostics at detection time. Reports to `mcp-awareness` via its tool API.

**Data sources:**

| Source | Metrics |
|--------|---------|
| DSM WebAPI (`SYNO.Core.System.Utilization`) | CPU, RAM, disk I/O, network, load averages |
| DSM WebAPI (`SYNO.Storage.CGI.Storage`) | Volume usage, disk temperature |
| Docker Engine / CLI | Container list, status, CPU/RAM per container, start times, events |
| Process table (diagnostics only) | Top N by CPU/RAM, captured only on alert |

**Reporting to awareness service:**

```python
# Periodic status report
await mcp_client.call_tool("report_status", {
    "source": "synology-nas",
    "tags": ["infra", "nas", "seedbox"],
    "metrics": collected_metrics,
    "inventory": docker_inventory,
    "ttl_sec": 120
})

# On alert fire
await mcp_client.call_tool("report_alert", {
    "source": "synology-nas",
    "tags": ["infra", "nas"],
    "alert_id": "cpu-warn-20260319T143200Z",
    "level": "warning",
    "alert_type": "threshold",
    "message": "CPU at 96.2% for 8+ minutes",
    "diagnostics": captured_diagnostics
})

# On structural anomaly
await mcp_client.call_tool("report_alert", {
    "source": "synology-nas",
    "tags": ["infra", "nas", "docker"],
    "alert_id": "struct-qbt-stopped-20260319T150000Z",
    "level": "warning",
    "alert_type": "structural",
    "message": "Expected container 'qbittorrent' is not running",
    "details": {"expected": "always_running", "actual_status": "exited", "exit_code": 137}
})
```

### Threshold Configuration

```yaml
# config.yaml (lives with the edge daemon, not the awareness service)
check_interval_sec: 60
alert_recheck_sec: 15
awareness_server: "http://localhost:8420"

thresholds:
  cpu_pct: { warn: 80, critical: 95, sustained_sec: 300 }
  ram_pct: { warn: 85, critical: 95, sustained_sec: 0 }
  disk_busy_pct: { warn: 85, critical: 95, sustained_sec: 120 }
  volume_used_pct: { warn: 80, critical: 90, sustained_sec: 0 }
  disk_temp_c: { warn: 50, critical: 60, sustained_sec: 0 }
  docker_restarts: { warn: 3, critical: 5, window_sec: 600 }
```

### Expected State Manifest

```yaml
# expected_state.yaml (lives with the edge daemon)
docker_containers:
  always_running: [qbittorrent, plex, download-station, qbt-cleanup]
  optional: [handbrake, calibre-web]

services:
  always_active: [smbd, synoscgi]

network:
  min_tx_kbps: 50
  sustained_sec: 1800

processes:
  expected_heavy:
    - name: qbittorrent
      typical_cpu_range: [20, 70]
      typical_mem_mb_range: [800, 2500]
```

### Deployment

```yaml
# docker-compose.yaml
services:
  mcp-awareness:
    build: ./mcp-awareness
    container_name: mcp-awareness
    restart: unless-stopped
    volumes:
      - ./awareness-data:/app/data
    ports:
      - "8420:8420"
    deploy:
      resources:
        limits: { cpus: '0.1', memory: 64M }

  homelab-edge:
    build: ./homelab-edge
    container_name: homelab-edge
    restart: unless-stopped
    depends_on: [mcp-awareness]
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock:ro
      - ./homelab-config.yaml:/app/config.yaml:ro
      - ./homelab-expected-state.yaml:/app/expected_state.yaml:ro
    environment:
      - DSM_HOST=localhost
      - DSM_USER=${DSM_ADMIN_USER}
      - DSM_PASS=${DSM_ADMIN_PASS}
      - AWARENESS_URL=http://mcp-awareness:8420
    deploy:
      resources:
        limits: { cpus: '0.25', memory: 128M }
```

---

## Future Source Providers

### Calendar Awareness Provider
- Reads Google Calendar API on a schedule
- Pre-computes: upcoming meetings, related email threads, travel time
- Reports with `source: gcal`, `tags: [calendar, work]`

### CI/CD Awareness Provider
- Webhooks from GitHub Actions / GitLab CI
- Monitors pipeline duration vs historical average, failure patterns
- Reports with `source: github-ci`, `tags: [cicd, deploy]`

### Database Awareness Provider
- Queries replication lag, connection counts, slow query log
- Structural detection: replication stalled, connection pool exhaustion
- Reports with `source: postgres-primary`, `tags: [database, postgres]`

### E-commerce Awareness Provider
- Queries order volume, SKU-level sales rates, checkout funnel completion
- Structural detection: top SKU absent from orders for N hours
- Reports with `source: shopify`, `tags: [ecommerce, orders]`

All use the same `report_status`, `report_alert`, `learn_pattern` tools.

---

## Implementation Priority

| Priority | Task | Effort | Value |
|----------|------|--------|-------|
| P0 | `mcp-awareness` service with JSON file store | Medium | Core platform |
| P0 | Generic data schema (source, tags, types) | Low | Contract definition |
| P0 | Read resources (status, alerts, knowledge, suppressions) | Low | Agent integration |
| P0 | Write tools (report_status, report_alert, learn_pattern, suppress_alert) | Medium | Edge + agent integration |
| P1 | Homelab edge daemon (DSM API + Docker) | Medium | First source provider |
| P1 | Expected state manifest + structural detection | Low | Layer 3 enablement |
| P1 | Diagnostic capture at detection time | Medium | Core differentiator |
| P1 | TTL-based status expiry (stale source detection) | Low | Reliability |
| P1 | Docker Compose deployment (awareness + first edge) | Low | Operability |
| P2 | SQLite backend (replace JSON file for scale) | Medium | Production readiness |
| P2 | Tag-based filtering on resources | Low | Query flexibility |
| P2 | Suppression with time-based expiry + escalation | Low | Noise reduction |
| P2 | Alert history with rotation | Low | Trend awareness |
| P2 | Baseline learning (rolling 24h averages) | Medium | Seedbox-aware alerting |
| P3 | Calendar awareness provider | Medium | Cross-domain proof |
| P3 | Trend detection + forecasting | High | Advanced alerting |
| P3 | Suggested remediation (tools with human confirmation) | Medium | Agentic capability |
| P3 | Proxmox / multi-system edge providers | Medium | Expanded scope |
| P3 | Streamable HTTP transport + OAuth | Medium | Remote access |

---

## Testing Strategy

### Unit Tests
- Data schema validation (all entry types)
- TTL expiry logic
- Tag-based filtering
- Suppression evaluation (expiry, escalation override)
- Pattern matching against conditions

### Integration Tests
- Edge daemon → awareness service tool calls
- MCP resource reads with filtering
- Concurrent writes from multiple sources
- Subscription notification delivery

### Smoke Tests
- Deploy awareness service + one edge daemon
- Verify status reporting and resource reads
- Simulate alert fire, verify diagnostics captured
- Stop an expected container, verify structural alert
- Test learn_pattern → read back via knowledge resource
- Test suppress_alert → verify alert filtered on next read

---

## Open Questions

1. **How do edge processes connect to the awareness service?** If both run on the same host, stdio works. If distributed, Streamable HTTP. Should the edge process be an MCP client, or call a simpler REST API alongside the MCP interface?

2. **Schema evolution**: As new source providers are added, `data` payloads will diverge. Should the service validate payloads against per-source schemas, or stay schema-free?

3. **Conflict resolution**: If two agents call `learn_pattern` with contradictory information, which wins? Last-write-wins? Versioned entries with conflict markers?

4. **Access control**: Should different agents have different read/write permissions? Edge daemons should write status/alerts but not suppressions. Agents should write patterns/suppressions but not fake alerts.

5. **Separate repos**: `mcp-awareness` (the generic service) and `homelab-edge` (the NAS-specific source provider) should be separate repositories. The service is the platform; the edge daemon is one consumer.

6. **Existing solutions check**: Before building from scratch, verify whether any existing project exposes a generic tagged alert/knowledge store via MCP. The edge daemon is custom regardless, but the store might not need to be.

---

*[mcp-awareness](https://github.com/cmeans/mcp-awareness) is open source under the [Apache 2.0 License](../LICENSE). Copyright (c) 2026 Chris Means.*
