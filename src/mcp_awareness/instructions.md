This server provides ambient awareness across monitored systems.
At conversation start, read awareness://briefing. If attention_needed
is true, mention the suggested_mention or compose your own from the
source headlines. If the user asks for details, drill into the
referenced resources. Don't read anything else unless asked or unless
the briefing indicates an issue. Group alerts by source if multiple
systems have issues. One sentence for warnings, short paragraph for
critical. Don't re-check unless asked. When you learn something worth
keeping, use remember for permanent facts, add_context for temporal
events, and learn_pattern ONLY for if/then rules (when X, expect Y).
When the user asks to suppress alerts, use suppress_alert — not a
memory edit.

Query discipline: before pulling full entries, call get_knowledge with
mode='list' to scan metadata. Always set limit (e.g., 10–20) to avoid
unbounded results. Use hint to re-rank by relevance so the best matches
come first. Narrow with 2–3 specific tags rather than one broad tag.
Use since/until for time-bounded queries. Call get_stats or get_tags
first if you're unsure how much data exists.
