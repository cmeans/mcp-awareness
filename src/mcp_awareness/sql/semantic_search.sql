/* name: semantic_search */
/* mode: templated */
/* Hybrid retrieval: fuse vector (HNSW) and lexical (FTS/GIN) results via
   Reciprocal Rank Fusion (k=60).  Either branch may be empty — an empty
   branch contributes zero rows to the UNION ALL, so the query gracefully
   degrades to vector-only or FTS-only depending on what's available.
   {{where}} — conditional WHERE clauses (e.owner_id, e.deleted IS NULL,
               optionally AND e.type/source/tags/updated filters)
   Params (positional):
     1: query_vector (vector_hits similarity)
     2: model        (embedding model name)
     3: query_vector (vector_hits ORDER BY)
     4: query_language (regconfig for plainto_tsquery)
     5: query_text     (text for plainto_tsquery)
     6: [...filter params from {{where}} — duplicated for both CTEs]
     7: limit
*/
WITH vector_hits AS (
  SELECT e.id, ROW_NUMBER() OVER (ORDER BY emb.embedding <=> %s::vector) AS rnk
  FROM entries e
  JOIN embeddings emb ON e.id = emb.entry_id AND emb.model = %s
  WHERE {where}
  ORDER BY emb.embedding <=> %s::vector
  LIMIT 50
),
lexical_hits AS (
  SELECT e.id, ROW_NUMBER() OVER (ORDER BY ts_rank_cd(e.tsv, q) DESC) AS rnk
  FROM entries e, plainto_tsquery(%s::regconfig, %s) q
  WHERE e.tsv @@ q AND {where}
  ORDER BY ts_rank_cd(e.tsv, q) DESC
  LIMIT 50
),
fused AS (
  SELECT id, SUM(1.0 / (60 + rnk)) AS score
  FROM (
    SELECT id, rnk FROM vector_hits
    UNION ALL
    SELECT id, rnk FROM lexical_hits
  ) r
  GROUP BY id
)
SELECT e.*, f.score AS similarity
FROM fused f JOIN entries e ON e.id = f.id
ORDER BY f.score DESC
LIMIT %s
