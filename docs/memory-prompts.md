# Memory Prompts

Connecting awareness as an MCP server gives your AI the tools — but without instructions, it won't know when or how to use them. Memory prompts bridge that gap: they tell your AI to check awareness at conversation start, store knowledge when it learns something, and maintain project status across platforms.

There are three levels of prompt integration, depending on your platform and use case.

## 1. Platform memory (Claude.ai, Claude Desktop, Claude mobile)

These platforms support memory instructions — persistent prompts that apply to every conversation. The awareness prompt is split into sections, each under 500 characters (Claude Desktop's limit per entry).

The prompt is stored in awareness itself. Any AI with access can retrieve it:

```
get_knowledge(source="awareness-prompt")
```

Or you can paste the sections into your platform's memory manually:

### Entry 1: Core

> awareness is a portable knowledge store (MCP server). Use it in conjunction with your memory for anything worth remembering. Start each conversation with get_briefing. Only mention if attention_needed is true. Be terse during coding. Always surface critical alerts immediately. If user says stop alerting, use suppress_alert.

### Entry 2: Reading

> Before answering questions that might have stored context, call get_knowledge with source/tags/entry_type filters. When entering a project, query by repo name tag. When user references a system, check get_status. Call get_stats before pulling everything to gauge store size. Check if other platforms left context about recent work.

### Entry 3: Writing

> Store knowledge: remember for general notes, learn_pattern for operational facts with conditions/effects, add_context for time-limited, set_preference for behavior. Set learned_from to your platform name. Set source to the subject (e.g. personal, nas, mcp-awareness-project) — source is how queries filter, so be consistent with existing sources. Use update_entry to modify entries in place — changes tracked in _changelog.

### Entry 4: Quality

> Call get_tags before creating new tags to prevent drift. Call get_knowledge with relevant tags before writing to deduplicate. When the user corrects you or reveals a preference, store to both your memory AND awareness. On work milestones (PR merged, bug fixed), write add_context so other platforms know. Tag with repo name for project knowledge.

### Entry 5: Tag conventions

> Use consistent tags: repo names (mcp-awareness, synology-mcp), domains (infra, personal, project, preference), systems (nas, homeassistant, garmin). Check get_tags for existing tags before creating new ones.

### Entry 6: Status

> Maintain a single permanent status note per project using remember, then update it in place with update_entry as work progresses. The _changelog tracks history automatically. Use tags ["project", "status"] plus the repo name. Don't create expiring status entries — use one living document per project that any agent can update.

### Entry 7: Resilience

> If a tool call fails with an unstructured error, retry once. If it fails again, the service may be restarting — continue your work and try again later. Do not repeatedly retry. Never discard user data silently — if a write fails, tell the user it didn't persist.

### Platform notes

- **Claude Desktop** has a 500-character limit per memory entry. Each section above fits within that limit.
- **Claude.ai** does not have the same limit — you can combine sections if preferred.
- **`learned_from`** should be set to your platform name (e.g., `"claude-ai"`, `"claude-code"`, `"claude-desktop"`). Some platforms self-identify differently — Claude.ai may use `"claude-web"`. Don't fight it; the field is for traceability, not filtering.

## 2. Global CLAUDE.md (Claude Code — all projects)

Claude Code reads `~/.claude/CLAUDE.md` at startup and applies it to every project. This is the best place for awareness instructions if you use Claude Code across multiple repos.

Example `~/.claude/CLAUDE.md`:

```markdown
## Awareness

`awareness` is a portable knowledge store connected as an MCP server. Use it
in conjunction with your auto-memory for anything worth remembering.

### Conversation lifecycle

- **Start of conversation:** Call `get_briefing`. Mention only if `attention_needed`
  is true. Be terse during coding — don't interrupt flow for all-clear briefings.
  Always surface critical alerts immediately.
- **User says stop alerting:** Use `suppress_alert`.

### Reading knowledge

- **User's question might have stored context:** Call `get_knowledge` before
  answering — especially for infrastructure, family, health, finances, or
  project history.
- **Entering a project:** Query `get_knowledge` with tags matching the repo name
  (e.g., `["mcp-awareness"]`, `["synology-mcp"]`).
- **User references a system by name:** Check `get_status` for that source.
- **Starting work:** Check if other platforms left context about what the user
  was doing (e.g., "was debugging X in Claude.ai earlier").

### Writing knowledge

- **User tells you something worth remembering:** Store it — `learn_pattern`
  for permanent operational facts, `remember` for general-purpose notes,
  `add_context` for time-limited, `set_preference` for behavior. Always set
  `learned_from` to `"claude-code"`.
- **Set `source` correctly:** Use a descriptive source that identifies the
  subject — e.g., `"mcp-awareness-project"` for project knowledge, `"personal"`
  for personal facts, `"nas"` for infrastructure. The `source` field is how
  queries filter entries, so be consistent with existing sources.
- **Cross-platform sync:** When the user corrects you or reveals a preference,
  store to both auto-memory AND awareness. Auto-memory is local to Claude Code;
  awareness is cross-platform so all agents benefit.
- **Tag with repo name:** When learning something about a project's architecture
  or the user's workflow, include the repo name in tags.
- **Deduplicate:** Call `get_knowledge` with relevant tags before writing to
  avoid storing what's already there.
- **Check existing tags:** Call `get_tags` before creating new tags to prevent
  drift (e.g., `"infrastructure"` vs `"infra"`).
- **Work milestones:** When finishing significant work (PR merged, release tagged,
  bug fixed), write an `add_context` entry so other platforms know what happened.
- **Maintain status:** Maintain a single permanent status note per project using
  `remember`, then update it in place with `update_entry` as work progresses.
  The `_changelog` tracks history automatically. Use tags `["project", "status"]`
  plus the repo name. Don't create expiring status entries — use one living
  document per project that any agent can update.

### Resilience

- **Retry once:** If a tool call fails with an unstructured error, retry once.
  If it fails again, the service may be restarting — continue your work and try
  again later. Do not repeatedly retry.
- **Never discard silently:** If a write fails, tell the user it didn't persist.

### Tag conventions

Use consistent tags so knowledge is findable across platforms:
- Repo names: `["mcp-awareness"]`, `["synology-mcp"]`, `["clipboard-mcp"]`
- Domains: `["infra"]`, `["personal"]`, `["project"]`, `["preference"]`
- Systems: `["nas"]`, `["homeassistant"]`, `["garmin"]`
```

### How it differs from platform memory

The global CLAUDE.md is more detailed because Claude Code has more context space and can follow longer instructions. It includes:
- Cross-platform sync guidance (store to both auto-memory and awareness)
- Deduplication (check before writing)
- Project status maintenance with `update_entry`
- Resilience (retry once, never discard silently)

## 3. Project CLAUDE.md (per-repo, checked into source)

For projects that use awareness, add a section to the repo's `CLAUDE.md` so any contributor's AI (in Claude Code, Cursor, or other MCP-aware editors) automatically integrates with awareness when working on that project.

Example (from the `mcp-awareness` repo):

```markdown
## Working with awareness

If you have access to the awareness MCP server while working on this repo:
- **Verify connection:** Call `get_briefing` at the start of work. If it fails
  or returns an unstructured error, awareness is not reachable — skip the
  remaining steps.
- **Check context:** Call `get_knowledge(tags=["mcp-awareness"])` to see if
  other agents or platforms left relevant context.
- **Maintain status:** Keep a single permanent status note for this project
  using `remember`, then update it with `update_entry` as work progresses.
  Use tags `["mcp-awareness", "project", "status"]`. The `_changelog` tracks
  history automatically.
- **Record milestones:** When finishing significant work (PR merged, release
  tagged), update the status note so other platforms know what happened.
```

### How it differs from the global prompt

The project CLAUDE.md is scoped and defensive:
- **Verify connection first** — not every contributor will have awareness configured
- **Project-specific tags** — uses the repo name in tags
- **Focused on project workflow** — maintain status, check context, record milestones
- **No personal knowledge instructions** — that belongs in the global prompt, not a shared repo

## Putting it all together

| Level | Where | Who sees it | What it does |
|-------|-------|-------------|-------------|
| Platform memory | Claude.ai / Desktop settings | That platform only | Core behavior: briefing, read, write, resilience |
| Global CLAUDE.md | `~/.claude/CLAUDE.md` | All Claude Code sessions | Detailed instructions: dedup, cross-platform sync, status |
| Project CLAUDE.md | `CLAUDE.md` in repo root | Anyone working on that repo | Project-specific: verify connection, maintain status, record milestones |

The three levels complement each other. Platform memory handles the basics. Global CLAUDE.md adds depth for Claude Code. Project CLAUDE.md ensures project-specific workflow without assuming awareness is available.
