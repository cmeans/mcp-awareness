/* name: semantic_search */
/* mode: templated */
/* Search entries by vector cosine similarity with optional filters.
   Returns entries with similarity score (1 - cosine_distance), sorted by relevance.
   {{where}} — conditional WHERE clauses (e.owner_id, e.deleted IS NULL,
               optionally AND e.type/source/tags/updated filters)
   Params: query_vector (for similarity calc), model,
           owner_id, [...filter params from {{where}}],
           query_vector (for ORDER BY), limit
*/
SELECT e.*, 1 - (emb.embedding <=> %s::vector) AS similarity FROM entries e JOIN embeddings emb ON e.id = emb.entry_id AND emb.model = %s WHERE {where} ORDER BY emb.embedding <=> %s::vector LIMIT %s
