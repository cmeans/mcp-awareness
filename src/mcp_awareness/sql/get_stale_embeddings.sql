/* name: get_stale_embeddings */
/* mode: literal */
/* Get entries with embeddings for a model, along with the stored text_hash.
   Python-side compares emb_text_hash against the current content hash
   to identify stale embeddings that need re-generation.
   Params: model, owner_id, limit
*/
SELECT e.*, emb.text_hash AS emb_text_hash FROM entries e JOIN embeddings emb ON e.id = emb.entry_id AND emb.model = %s WHERE e.owner_id = %s AND e.deleted IS NULL ORDER BY COALESCE(e.updated, e.created) DESC LIMIT %s
