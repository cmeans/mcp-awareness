# PoC Demo: End-to-End Awareness Briefing

This guide walks through the complete proof of concept — from starting the server to seeing an ambient awareness alert surface during an unrelated conversation in Claude.ai.

## Prerequisites

- Python 3.10+ with `mcp-awareness` installed (`pip install -e .`)
- [cloudflared](https://github.com/cloudflare/cloudflared/releases) installed (for remote access)
- A [Claude.ai](https://claude.ai) account (free tier works)

## Step 1: Start the server

```bash
# Create a data directory
mkdir -p /tmp/awareness-demo

# Start the server with HTTP transport
AWARENESS_TRANSPORT=streamable-http AWARENESS_DATA_DIR=/tmp/awareness-demo mcp-awareness
```

You should see:
```
INFO:     Uvicorn running on http://0.0.0.0:8420 (Press CTRL+C to quit)
```

## Step 2: Populate the store with demo data

In a second terminal:

```bash
python examples/simulate_edge.py --data-dir /tmp/awareness-demo
```

This creates a NAS status entry, fires and resolves a CPU alert, and stores a learned pattern. The store ends in an all-clear state.

To add an active alert for the demo:

```bash
python3 -c "
import sys; sys.path.insert(0, 'src')
from mcp_awareness.store import AwarenessStore
store = AwarenessStore('/tmp/awareness-demo/awareness.db')
store.upsert_status('synology-nas', ['infra', 'nas', 'seedbox'], {
    'metrics': {'cpu': {'usage_pct': 45}, 'memory': {'usage_pct': 78}},
    'inventory': {'docker': {'running': ['plex', 'download-station'], 'stopped': ['qbittorrent']}},
    'ttl_sec': 7200,
})
store.upsert_alert('synology-nas', ['infra', 'nas', 'docker'], 'struct-qbt-stopped', {
    'alert_id': 'struct-qbt-stopped',
    'level': 'warning',
    'alert_type': 'structural',
    'message': 'Expected container qbittorrent is not running — disk I/O dropped to 12% (normally 80-90%)',
    'details': {'expected': 'always_running', 'actual_status': 'exited', 'exit_code': 137},
    'resolved': False,
})
print('Alert created.')
"
```

## Step 3: Expose via Cloudflare Tunnel

In a third terminal:

```bash
cloudflared tunnel --url http://localhost:8420
```

Look for the generated URL in the output:
```
Your quick Tunnel has been created! Visit it at:
https://some-random-words.trycloudflare.com
```

Note this URL — you'll need it in the next step. The tunnel URL changes each time you restart cloudflared.

## Step 4: Add the MCP connector in Claude.ai

1. Go to [claude.ai](https://claude.ai)
2. Open **Settings** → **Integrations**
3. Click **Add Custom Connector** (or similar)
4. Enter a name: `mcp-awareness`
5. Enter the URL: `https://your-tunnel-url.trycloudflare.com/mcp`
6. Save and ensure all tools are permitted/allowed

## Step 5: Add the memory instruction

Start a new conversation in Claude.ai and paste:

> Add this to your Memory:
>
> At the start of each conversation, call get_briefing from the mcp-awareness server. If attention_needed is true, briefly mention the suggested_mention or compose your own from the source headlines before responding to my question. If attention_needed is false, say nothing about it. Don't re-check unless I ask. When you learn something about one of my systems from conversation, call learn_pattern to record it. When I ask you to stop alerting about something, use suppress_alert — don't use your own memory for operational knowledge.

Claude should confirm it saved the memory instruction.

## Step 6: Test it

Start a **new conversation** and ask something unrelated:

> What's the weather like this weekend?

You should see Claude:
1. Call `get_briefing` (may show in the thinking/tool use area)
2. Mention the qBittorrent alert as an FYI before answering your weather question
3. Answer the weather question normally

Example output:

> **FYI:** qbittorrent container appears to be down on the NAS — disk I/O has dropped to ~12% (normally 80-90%).
>
> Weekend looks decent for Chicago: Saturday high of ~61°F...

![Demo screenshot](images/android-briefing-demo.png)

## Step 7: Test suppression

In the same or a new conversation, say:

> I know about the qBittorrent issue, suppress it for now

Claude should call `suppress_alert` with appropriate parameters (source, tags, duration). Subsequent new conversations should no longer mention the qBittorrent alert.

## What's happening under the hood

```
You (Claude.ai) → Cloudflare Tunnel → mcp-awareness HTTP server
                                            │
                                    ┌───────┴────────┐
                                    │   get_briefing  │
                                    │   tool call     │
                                    └───────┬────────┘
                                            │
                                    ┌───────┴────────┐
                                    │   Collator      │
                                    │   • reads store │
                                    │   • applies     │
                                    │     suppressions│
                                    │   • generates   │
                                    │     briefing    │
                                    └───────┬────────┘
                                            │
                                    JSON briefing returned
                                    (~200 tokens all-clear,
                                     ~500 with issues)
```

## Notes

- **Tunnel URLs are temporary** — they change each time you restart cloudflared. You'll need to update the Claude.ai connector each time.
- **The store persists** in the data directory. Restart the server and your data is still there.
- **Claude.ai uses tools, not resources** — the read path goes through `get_briefing`, `get_alerts`, etc. (tools), not the `awareness://` resources. This is a Claude.ai limitation; Claude Code and other MCP clients may support resources directly.
- **Suppression matching is content-aware** — a suppression tagged `["qbittorrent"]` will match alerts whose alert_id or message contains "qbittorrent", even if the alert's structural tags are different.
