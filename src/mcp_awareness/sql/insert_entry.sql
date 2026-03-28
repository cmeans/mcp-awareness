-- _insert_entry: INSERT a new entry into the entries table
INSERT INTO entries
   (id, owner_id, type, source, created, updated, expires, tags, data, logical_key)
   VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s)
