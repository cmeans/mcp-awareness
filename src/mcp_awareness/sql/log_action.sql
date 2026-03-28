-- log_action: INSERT an action-tracking record
INSERT INTO actions (owner_id, entry_id, timestamp, platform, action, detail, tags) VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb) RETURNING id
