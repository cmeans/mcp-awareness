/* name: find_schema */
/* mode: literal */
/* Look up a schema entry by logical_key, preferring caller-owned over _system.
   Returns the caller's own version if present, otherwise the _system version.
   Soft-deleted entries are excluded.
   Params: logical_key, caller (owner_id), caller (owner_id again for ORDER BY)
*/
SELECT id, type, source, tags, created, updated, expires, data, logical_key, owner_id, language, deleted
FROM entries
WHERE type = 'schema'
  AND logical_key = %s
  AND owner_id IN (%s, '_system')
  AND deleted IS NULL
ORDER BY CASE WHEN owner_id = %s THEN 0 ELSE 1 END
LIMIT 1
