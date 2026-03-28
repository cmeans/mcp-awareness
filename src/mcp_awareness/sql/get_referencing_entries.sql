/* name: get_referencing_entries */
/* mode: literal */
/* Placeholder file — get_referencing_entries is implemented via _query_entries
   with WHERE clause "data->'related_ids' @> (entry_id)::jsonb".
   No SQL in this file; see _query_entries and postgres_store.py.
   Params (via _query_entries): owner_id, entry_id (as jsonb array)
*/
