/* name: soft_delete_by_source */
/* mode: templated */
/* Soft-delete all entries for a source, optionally filtered by type.
   Saves original expires into data._original_expires for later restoration.
   {{where}} — conditional WHERE clauses (owner_id, source, deleted IS NULL,
               optionally type)
   Params: deleted (now), expires (trash_expires), owner_id, source, [type]
*/
UPDATE entries SET
 data = CASE WHEN expires IS NOT NULL
   THEN jsonb_set(data, '{{_original_expires}}', to_jsonb(expires))
   ELSE data - '_original_expires' END,
 deleted = %s, expires = %s WHERE {where}
