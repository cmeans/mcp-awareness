/* name: get_all_patterns */
/* mode: literal */
/* Get all pattern entries.
   Params: owner_id, type
*/
SELECT * FROM entries
WHERE owner_id = %s AND type = %s AND deleted IS NULL
ORDER BY COALESCE(updated, created) DESC
