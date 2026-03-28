-- upsert_by_logical_key: INSERT with ON CONFLICT for logical_key upsert
INSERT INTO entries
   (id, owner_id, type, source, created, updated, expires, tags, data, logical_key)
   VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s)
   ON CONFLICT (owner_id, source, logical_key)
       WHERE logical_key IS NOT NULL AND deleted IS NULL
   DO UPDATE SET id = entries.id
   RETURNING (xmax = 0) AS inserted
