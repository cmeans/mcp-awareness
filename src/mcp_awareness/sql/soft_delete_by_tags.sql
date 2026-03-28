/* name: soft_delete_by_tags */
/* mode: templated */
/* Soft-delete all entries matching ALL given tags (AND logic).
   Saves original expires into data._original_expires for later restoration.
   {{tag_clauses}} — "tags @> (tag)::jsonb" repeated per tag with AND logic
   Params: deleted (now), expires (trash_expires), owner_id, ...tag values (one jsonb array per tag)
*/
UPDATE entries SET
 data = CASE WHEN expires IS NOT NULL
   THEN jsonb_set(data, '{{_original_expires}}', to_jsonb(expires))
   ELSE data - '_original_expires' END,
 deleted = %s, expires = %s
 WHERE owner_id = %s AND deleted IS NULL AND {tag_clauses}
