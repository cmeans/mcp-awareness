/* name: upsert_embedding */
/* mode: literal */
/* Insert or update an embedding for an entry + model pair.
   On conflict (same entry_id + model), updates the vector, text_hash,
   dimensions, and resets created to now().
   Params: owner_id, entry_id, model, dimensions, text_hash, embedding (vector literal)
*/
INSERT INTO embeddings (owner_id, entry_id, model, dimensions, text_hash, embedding) VALUES (%s, %s, %s, %s, %s, %s::vector) ON CONFLICT (entry_id, model) DO UPDATE SET embedding = EXCLUDED.embedding, text_hash = EXCLUDED.text_hash, dimensions = EXCLUDED.dimensions, created = now()
