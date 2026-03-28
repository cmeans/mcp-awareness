-- update_intention_state: UPDATE an intention's data and timestamp
UPDATE entries SET updated = %s, data = %s::jsonb WHERE id = %s
