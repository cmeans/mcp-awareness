/* name: soft_delete_by_id */
/* mode: literal */
/* Soft-delete a single entry by ID. Saves the original expires value
   into data._original_expires for later restoration, then sets deleted
   timestamp and a new expires for trash retention (30 days).
   Params: deleted (now), expires (trash_expires), owner_id, id
*/
UPDATE entries SET
 data = CASE WHEN expires IS NOT NULL
   THEN jsonb_set(data, '{_original_expires}', to_jsonb(expires))
   ELSE data - '_original_expires' END,
 deleted = %s, expires = %s
 WHERE owner_id = %s AND id = %s AND deleted IS NULL
