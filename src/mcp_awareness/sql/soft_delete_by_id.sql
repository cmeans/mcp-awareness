-- soft_delete_by_id: soft-delete a single entry, preserving original expires
UPDATE entries SET
 data = CASE WHEN expires IS NOT NULL
   THEN jsonb_set(data, '{_original_expires}', to_jsonb(expires))
   ELSE data - '_original_expires' END,
 deleted = %s, expires = %s
 WHERE owner_id = %s AND id = %s AND deleted IS NULL
