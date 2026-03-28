/* name: restore_by_tags */
/* mode: templated */
/* Restore soft-deleted entries matching ALL given tags (AND logic).
   Recovers original expires from data._original_expires.
   {{tag_clauses}} — "tags @> (tag)::jsonb" repeated per tag with AND logic
   Params: owner_id, ...tag values (one jsonb array per tag)
*/
UPDATE entries SET deleted = NULL,
 expires = (data->>'_original_expires')::timestamptz,
 data = data - '_original_expires'
 WHERE owner_id = %s AND deleted IS NOT NULL AND {tag_clauses}
