-- mcp-awareness demo seed data
-- Runs once on first startup when the database is empty.
-- Everything here is true about the demo instance itself — no fake data.

-- ── Status: this instance ───────────────────────────────────────────────────

INSERT INTO entries (id, type, source, created, updated, tags, data, logical_key)
VALUES (
  'demo-status-001',
  'status',
  'mcp-awareness',
  NOW(), NOW(),
  '["demo", "status"]',
  '{"status": "ok", "message": "Demo instance running. Seeded with starter data to explore. Ask your AI to use the getting-started prompt to personalize this instance."}',
  'instance-status'
);

-- ── Alert: welcome ──────────────────────────────────────────────────────────

INSERT INTO entries (id, type, source, created, updated, tags, data)
VALUES (
  'demo-alert-001',
  'alert',
  'mcp-awareness',
  NOW(), NOW(),
  '["demo", "onboarding"]',
  '{"alert_id": "welcome", "severity": "info", "message": "Welcome to mcp-awareness! This instance is seeded with demo data. Explore the tools, try the prompts, then make it yours.", "alert_type": "onboarding"}'
);

-- ── Notes: what awareness is and how to use it ──────────────────────────────

INSERT INTO entries (id, type, source, created, updated, tags, data, logical_key)
VALUES (
  'demo-note-001',
  'note',
  'mcp-awareness',
  NOW(), NOW(),
  '["demo", "getting-started"]',
  '{"description": "What is mcp-awareness?", "content": "mcp-awareness is shared memory for every AI you use. Any AI assistant can store and retrieve knowledge through it using the Model Context Protocol (MCP). What you teach one AI, every AI knows — permanently, portably, privately.\n\nThis instance is yours. It runs on your machine, your data stays local (in Docker volumes), and you control what goes in and comes out.", "content_type": "text/plain"}',
  'what-is-awareness'
);

INSERT INTO entries (id, type, source, created, updated, tags, data, logical_key)
VALUES (
  'demo-note-002',
  'note',
  'mcp-awareness',
  NOW(), NOW(),
  '["demo", "getting-started"]',
  '{"description": "Tools you can try", "content": "WRITING:\n- remember — store anything worth keeping (facts, notes, configs)\n- learn_pattern — record operational knowledge with conditions/effects\n- add_context — store time-limited info that auto-expires\n- set_preference — store behavioral preferences\n- report_status / report_alert — report system state\n\nREADING:\n- get_briefing — compact summary, check at conversation start\n- get_knowledge — retrieve by source, tags, or type\n- get_alerts / get_status — drill into specifics\n- get_stats — entry counts and sources\n- get_tags — all tags with usage counts\n\nMANAGING:\n- update_entry — modify in place with changelog tracking\n- delete_entry — soft delete (30-day trash, restorable)\n- restore_entry — recover from trash\n- suppress_alert — silence noisy alerts with expiry", "content_type": "text/plain"}',
  'tools-overview'
);

INSERT INTO entries (id, type, source, created, updated, tags, data, logical_key)
VALUES (
  'demo-note-003',
  'note',
  'mcp-awareness',
  NOW(), NOW(),
  '["demo", "getting-started"]',
  '{"description": "How cross-platform sync works", "content": "Every AI you connect to this instance shares the same knowledge base.\n\nExample flow:\n1. Tell Claude Desktop about your home network setup\n2. Ask Claude Code to check what it knows about your network — it sees the same data\n3. Open Claude.ai on your phone — same knowledge, no copy-paste\n\nThe trick: connect all your AI tools to the same awareness instance. Each one reads and writes to the same store. Knowledge flows automatically.", "content_type": "text/plain"}',
  'cross-platform-sync'
);

INSERT INTO entries (id, type, source, created, updated, tags, data, logical_key)
VALUES (
  'demo-note-004',
  'note',
  'mcp-awareness',
  NOW(), NOW(),
  '["demo", "getting-started"]',
  '{"description": "Ideas for what to store", "content": "Personal:\n- Your name, role, and how you like your AI to communicate\n- Family members, pets, important dates\n- Health info, allergies, medications\n\nProjects:\n- What you''re working on and current status\n- Architecture decisions and why you made them\n- Links to docs, repos, dashboards\n\nInfrastructure:\n- Network layout, server specs, service locations\n- Known quirks and troubleshooting steps\n- Backup schedules and maintenance windows\n\nPreferences:\n- Communication style (terse vs detailed)\n- Code style preferences\n- Tools and workflows you use", "content_type": "text/plain"}',
  'what-to-store'
);

INSERT INTO entries (id, type, source, created, updated, tags, data, logical_key)
VALUES (
  'demo-note-005',
  'note',
  'mcp-awareness',
  NOW(), NOW(),
  '["demo", "getting-started"]',
  '{"description": "About prompts", "content": "mcp-awareness includes 5 built-in prompts that generate dynamic content from your stored data:\n\n- agent_instructions — workflow guide for AI agents\n- project_context — knowledge and status for a specific project\n- system_status — status and alerts for a monitored system\n- write_guide — shows existing sources, tags, and entry types\n- catchup — what changed in the last N hours\n\nYou can also create your own prompts by storing entries with source=\"custom-prompt\". Template variables like {{project}} become prompt arguments. Custom prompts appear under the user/ namespace.\n\nThis instance includes two example custom prompts: user/daily-standup and user/incident-report.", "content_type": "text/plain"}',
  'about-prompts'
);

-- ── Context: time-limited entries (show TTL in action) ──────────────────────

INSERT INTO entries (id, type, source, created, updated, expires, tags, data)
VALUES (
  'demo-context-001',
  'context',
  'mcp-awareness',
  NOW(), NOW(),
  NOW() + INTERVAL '7 days',
  '["demo"]',
  '{"description": "This demo data self-destructs in 7 days. By then you will have added your own knowledge. Use delete_entry with tags=[\"demo\"] to clean it up sooner."}'
);

-- ── Preferences ─────────────────────────────────────────────────────────────

INSERT INTO entries (id, type, source, created, updated, tags, data, logical_key)
VALUES (
  'demo-pref-001',
  'preference',
  'mcp-awareness',
  NOW(), NOW(),
  '["demo", "preference"]',
  '{"key": "alert_verbosity", "scope": "global", "value": "brief", "description": "Keep alert mentions short — one sentence for warnings, short paragraph for critical."}',
  'alert_verbosity'
);

-- ── Patterns (show the intelligence layer) ──────────────────────────────────

INSERT INTO entries (id, type, source, created, updated, tags, data, logical_key)
VALUES (
  'demo-pattern-001',
  'pattern',
  'mcp-awareness',
  NOW(), NOW(),
  '["demo", "pattern"]',
  '{"description": "Example pattern: scheduled backups cause CPU spikes", "conditions": "CPU usage above 80% between 02:00-04:00", "effects": "Normal behavior — nightly backup job runs during this window. Do not alert.", "learned_from": "demo-seed"}',
  'backup-cpu-pattern'
);

-- ── Custom prompts ──────────────────────────────────────────────────────────

INSERT INTO entries (id, type, source, created, updated, tags, data, logical_key)
VALUES (
  'demo-prompt-001',
  'note',
  'custom-prompt',
  NOW(), NOW(),
  '["demo", "prompt"]',
  '{"description": "Daily standup summary", "content": "Review what I worked on recently for {{project}}. Summarize progress, flag any blockers: {{blocker}}. Keep it concise — this is for a standup.", "content_type": "text/plain"}',
  'daily-standup'
);

INSERT INTO entries (id, type, source, created, updated, tags, data, logical_key)
VALUES (
  'demo-prompt-002',
  'note',
  'custom-prompt',
  NOW(), NOW(),
  '["demo", "prompt"]',
  '{"description": "Incident report template", "content": "Write an incident report for {{system}}.\nSeverity: {{severity}}\nDescription: {{description}}\n\nInclude: timeline, impact, root cause (if known), and next steps.", "content_type": "text/plain"}',
  'incident-report'
);

-- ── Onboarding prompt (the self-populating magic) ───────────────────────────

INSERT INTO entries (id, type, source, created, updated, tags, data, logical_key)
VALUES (
  'demo-prompt-003',
  'note',
  'custom-prompt',
  NOW(), NOW(),
  '["demo", "prompt", "onboarding"]',
  '{"description": "Personalize your awareness instance", "content": "I just set up mcp-awareness and want to make it useful. Interview me to learn about:\n\n1. **Who I am** — name, role, how I like my AI to communicate\n2. **What I am working on** — current projects, goals, deadlines\n3. **My setup** — infrastructure, tools, services I use\n4. **Preferences** — communication style, alert verbosity, quiet hours\n\nAsk me one topic at a time. After each answer, store what I tell you using the appropriate awareness tool (remember, set_preference, etc). Tag everything with my name so it is easy to find.\n\nWhen we are done, give me a summary of what you stored.", "content_type": "text/plain"}',
  'getting-started'
);

-- ── Awareness prompt entries (so agent_instructions works) ──────────────────

INSERT INTO entries (id, type, source, created, updated, tags, data, logical_key)
VALUES (
  'demo-awareness-001',
  'note',
  'awareness-prompt',
  NOW(), NOW(),
  '["memory-prompt", "demo"]',
  '{"description": "Awareness prompt Entry 1 (Conversation Start):\nAt the start of each conversation, call get_briefing. If attention_needed is true, mention the key points. If everything is clear, proceed without comment — don''t waste tokens on all-clear reports."}',
  'prompt-entry-1'
),
(
  'demo-awareness-002',
  'note',
  'awareness-prompt',
  NOW(), NOW(),
  '["memory-prompt", "demo"]',
  '{"description": "Awareness prompt Entry 2 (Reading Knowledge):\nBefore answering questions about the user''s setup, projects, or preferences, call get_knowledge with relevant tags. The store may have context from other AI platforms that helps you give a better answer."}',
  'prompt-entry-2'
),
(
  'demo-awareness-003',
  'note',
  'awareness-prompt',
  NOW(), NOW(),
  '["memory-prompt", "demo"]',
  '{"description": "Awareness prompt Entry 3 (Writing Knowledge):\nWhen the user tells you something worth remembering, store it. Use remember for facts and notes, learn_pattern for operational knowledge, add_context for time-limited info, set_preference for behavioral settings. Always set learned_from to your platform name."}',
  'prompt-entry-3'
),
(
  'demo-awareness-004',
  'note',
  'awareness-prompt',
  NOW(), NOW(),
  '["memory-prompt", "demo"]',
  '{"description": "Awareness prompt Entry 4 (Tags):\nCall get_tags before creating new tags. Use existing tags over creating new ones. Tags are for retrieval, not description. Use singular forms (infra not infrastructure). Common tags: personal, project, preference, infra."}',
  'prompt-entry-4'
);
