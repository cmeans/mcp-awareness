-- get_stale_embeddings: SELECT entries with outdated embedding text_hash
SELECT e.*, emb.text_hash AS emb_text_hash FROM entries e JOIN embeddings emb ON e.id = emb.entry_id AND emb.model = %s WHERE e.owner_id = %s AND e.deleted IS NULL ORDER BY e.updated DESC LIMIT %s
