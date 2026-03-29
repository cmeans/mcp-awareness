/* name: clear_embeddings */
/* mode: literal */
/* Delete all embeddings for a given owner. Params: owner_id */
DELETE FROM embeddings WHERE owner_id = %s
