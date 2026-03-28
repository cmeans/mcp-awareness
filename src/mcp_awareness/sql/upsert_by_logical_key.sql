/* name: upsert_by_logical_key */
/* mode: literal */
/* Insert an entry with ON CONFLICT handling for source + logical_key upsert.
   If a matching active entry exists (same owner_id, source, logical_key),
   the insert becomes a no-op and returns inserted=false. The caller then
   computes a diff and calls update_entry if needed.
   Params: id, owner_id, type, source, created, updated, expires, tags (jsonb),
           data (jsonb), logical_key
   Returns: inserted (boolean) — true if row was inserted, false if conflict
*/
INSERT INTO entries
   (id, owner_id, type, source, created, updated, expires, tags, data, logical_key)
   VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s)
   ON CONFLICT (owner_id, source, logical_key)
       WHERE logical_key IS NOT NULL AND deleted IS NULL
   DO UPDATE SET id = entries.id
   RETURNING (xmax = 0) AS inserted
