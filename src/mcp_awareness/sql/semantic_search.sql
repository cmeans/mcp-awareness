-- semantic_search: SELECT entries by vector similarity with optional filters
SELECT e.*, 1 - (emb.embedding <=> %s::vector) AS similarity FROM entries e JOIN embeddings emb ON e.id = emb.entry_id AND emb.model = %s WHERE {where} ORDER BY emb.embedding <=> %s::vector LIMIT %s
