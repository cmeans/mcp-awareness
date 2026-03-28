-- upsert_alert: UPDATE an existing alert entry
UPDATE entries SET updated = %s, tags = %s::jsonb, data = %s::jsonb WHERE id = %s
