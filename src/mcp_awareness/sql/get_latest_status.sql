/* name: get_latest_status */
/* mode: literal */
/* Get the most recent active status entry for a given source.
   Params: owner_id, type, source
*/
SELECT * FROM entries WHERE owner_id = %s AND type = %s AND source = %s AND deleted IS NULL ORDER BY created DESC LIMIT 1
