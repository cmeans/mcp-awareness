/* name: get_cleanup_opted_in_owners */
/* mode: literal */
/* Find all owners who have opted in to auto-cleanup via the auto_cleanup preference.
   Returns distinct owner_ids where the preference value is "true".
   No params.
*/
SELECT DISTINCT owner_id FROM entries
 WHERE type = 'preference' AND deleted IS NULL
   AND data->>'key' = 'auto_cleanup' AND data->>'value' = 'true'
