-- restore_by_id: restore a soft-deleted entry, recovering original expires
UPDATE entries SET deleted = NULL,
 expires = (data->>'_original_expires')::timestamptz,
 data = data - '_original_expires'
 WHERE owner_id = %s AND id = %s AND deleted IS NOT NULL
