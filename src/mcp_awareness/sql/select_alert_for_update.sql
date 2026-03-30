/* name: select_alert_for_update */
/* mode: literal */
/* Lock an existing alert row for atomic upsert.
   Params: owner_id, type, source, alert_id
*/
SELECT * FROM entries
WHERE owner_id = %s AND type = %s AND source = %s AND data->>'alert_id' = %s AND deleted IS NULL
ORDER BY COALESCE(updated, created) DESC
FOR UPDATE
