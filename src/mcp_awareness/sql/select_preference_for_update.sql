/* name: select_preference_for_update */
/* mode: literal */
/* Lock an existing preference row for atomic upsert.
   Params: owner_id, type, key, scope
*/
SELECT * FROM entries
WHERE owner_id = %s AND type = %s AND data->>'key' = %s AND data->>'scope' = %s AND deleted IS NULL
ORDER BY COALESCE(updated, created) DESC
FOR UPDATE
