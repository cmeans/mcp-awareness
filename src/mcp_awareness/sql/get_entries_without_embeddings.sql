-- get_entries_without_embeddings: SELECT entries missing embeddings for a model
SELECT e.* FROM entries e LEFT JOIN embeddings emb ON e.id = emb.entry_id AND emb.model = %s WHERE e.owner_id = %s AND e.deleted IS NULL AND emb.id IS NULL AND e.type != %s ORDER BY e.updated DESC LIMIT %s
