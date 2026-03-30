/* name: get_all_active_alerts */
/* mode: literal */
/* Get all non-resolved, non-deleted alert entries.
   Params: owner_id, type
*/
SELECT * FROM entries
WHERE owner_id = %s AND type = %s AND deleted IS NULL
  AND NOT (data @> '{"resolved": true}'::jsonb)
  AND (expires IS NULL OR expires > NOW())
ORDER BY COALESCE(updated, created) DESC
