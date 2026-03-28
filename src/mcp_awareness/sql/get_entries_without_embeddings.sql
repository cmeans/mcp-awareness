/* name: get_entries_without_embeddings */
/* mode: literal */
/* Find active entries that have no embedding for the given model.
   Excludes suppression entries (short-lived, not worth embedding).
   Params: model, owner_id, type (suppression, to exclude), limit
*/
SELECT e.* FROM entries e LEFT JOIN embeddings emb ON e.id = emb.entry_id AND emb.model = %s WHERE e.owner_id = %s AND e.deleted IS NULL AND emb.id IS NULL AND e.type != %s ORDER BY e.updated DESC LIMIT %s
