/* name: get_all_active_suppressions */
/* mode: literal */
/* Get all non-expired suppression entries.
   Params: owner_id, type
*/
SELECT * FROM entries
WHERE owner_id = %s AND type = %s AND deleted IS NULL
  AND (expires IS NULL OR expires > NOW())
ORDER BY COALESCE(updated, created) DESC
