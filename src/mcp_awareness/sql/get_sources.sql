/* name: get_sources */
/* mode: literal */
/* Get all unique sources that have reported status.
   Params: owner_id, type
*/
SELECT DISTINCT source FROM entries WHERE owner_id = %s AND type = %s AND deleted IS NULL
