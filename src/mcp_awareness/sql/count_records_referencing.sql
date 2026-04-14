/* name: count_records_referencing */
/* mode: literal */
/* Count records referencing a schema version (for deletion-protection checks).
   schema_logical_key is decomposed at the Python layer into (schema_ref, schema_version)
   via rpartition(":") — schema_ref may itself contain ':' (e.g. "schema:edge-manifest").
   Params: owner_id, schema_ref, schema_version
*/
SELECT COUNT(*) AS cnt
FROM entries
WHERE type = 'record'
  AND owner_id = %s
  AND data->>'schema_ref' = %s
  AND data->>'schema_version' = %s
  AND deleted IS NULL
