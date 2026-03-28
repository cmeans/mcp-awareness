/* name: get_activity */
/* mode: templated */
/* Get combined read + action activity feed as a chronological UNION.
   {{where_r}} — WHERE clauses for the reads subquery (owner_id, optionally timestamp/platform)
   {{where_a}} — WHERE clauses for the actions subquery (owner_id, optionally timestamp/platform)
   {{limit_clause}} — "LIMIT N" hardcoded by caller (not a bind param)
   Params: ...reads params (owner_id, [since], [platform]),
           ...actions params (owner_id, [since], [platform])
*/
SELECT 'read' AS event_type, entry_id, timestamp, platform, tool_used AS detail, NULL AS action, '[]'::jsonb AS tags FROM reads WHERE {where_r} UNION ALL SELECT 'action' AS event_type, entry_id, timestamp, platform, detail, action, tags FROM actions WHERE {where_a} ORDER BY timestamp DESC {limit_clause}
