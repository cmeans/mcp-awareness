/* name: restore_by_id */
/* mode: literal */
/* Restore a soft-deleted entry by ID. Recovers the original expires value
   from data._original_expires and clears the deleted timestamp.
   Params: owner_id, id
*/
UPDATE entries SET deleted = NULL,
 expires = (data->>'_original_expires')::timestamptz,
 data = data - '_original_expires'
 WHERE owner_id = %s AND id = %s AND deleted IS NOT NULL
