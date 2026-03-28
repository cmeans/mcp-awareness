-- restore_by_tags: restore soft-deleted entries matching ALL given tags
UPDATE entries SET deleted = NULL,
 expires = (data->>'_original_expires')::timestamptz,
 data = data - '_original_expires'
 WHERE owner_id = %s AND deleted IS NOT NULL AND {tag_clauses}
