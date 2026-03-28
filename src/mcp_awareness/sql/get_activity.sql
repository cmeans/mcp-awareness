-- get_activity: SELECT combined read + action activity feed
SELECT 'read' AS event_type, entry_id, timestamp, platform, tool_used AS detail, NULL AS action, '[]'::jsonb AS tags FROM reads WHERE {where_r} UNION ALL SELECT 'action' AS event_type, entry_id, timestamp, platform, detail, action, tags FROM actions WHERE {where_a} ORDER BY timestamp DESC {limit_clause}
