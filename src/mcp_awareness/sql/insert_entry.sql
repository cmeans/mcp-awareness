/* name: _insert_entry */
/* mode: literal */
/* Insert a new entry into the entries table.
   Params: id, owner_id, type, source, created, updated, expires, tags (jsonb),
           data (jsonb), logical_key
*/
INSERT INTO entries
   (id, owner_id, type, source, created, updated, expires, tags, data, logical_key)
   VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s)
