-- upsert_embedding: INSERT or UPDATE an embedding for an entry + model pair
INSERT INTO embeddings (owner_id, entry_id, model, dimensions, text_hash, embedding) VALUES (%s, %s, %s, %s, %s, %s::vector) ON CONFLICT (entry_id, model) DO UPDATE SET embedding = EXCLUDED.embedding, text_hash = EXCLUDED.text_hash, dimensions = EXCLUDED.dimensions, created = now()
