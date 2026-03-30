/* name: get_all_statuses */
/* mode: literal */
/* Get the latest status entry per source using DISTINCT ON.
   Params: owner_id, type
*/
SELECT DISTINCT ON (source) * FROM entries
WHERE owner_id = %s AND type = %s AND deleted IS NULL
ORDER BY source, created DESC
