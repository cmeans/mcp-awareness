-- update_entry: UPDATE an entry's mutable fields with changelog tracking
UPDATE entries SET updated = %s, source = %s, tags = %s::jsonb, data = %s::jsonb WHERE id = %s
