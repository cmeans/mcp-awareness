-- soft_delete_by_source: soft-delete entries for a source, optionally filtered by type
UPDATE entries SET
 data = CASE WHEN expires IS NOT NULL
   THEN jsonb_set(data, '{{_original_expires}}', to_jsonb(expires))
   ELSE data - '_original_expires' END,
 deleted = %s, expires = %s WHERE {where}
