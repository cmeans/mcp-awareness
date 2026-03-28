/* name: upsert_status_delete */
/* mode: literal */
/* Delete existing active status entry for a source before inserting a replacement.
   One active status per source — this is the "delete" half of the upsert.
   Params: owner_id, type, source
*/
DELETE FROM entries WHERE owner_id = %s AND type = %s AND source = %s AND deleted IS NULL
