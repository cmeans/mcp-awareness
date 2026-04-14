/* name: list_records_referencing_ids */
/* mode: literal */
/* Returns up to 10 record ids referencing a schema version, for deletion-blocker detail.
   Params: owner_id, schema_ref, schema_version
*/
SELECT id
FROM entries
WHERE type = 'record'
  AND owner_id = %s
  AND data->>'schema_ref' = %s
  AND data->>'schema_version' = %s
  AND deleted IS NULL
ORDER BY created
LIMIT 10
