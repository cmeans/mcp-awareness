/* name: count_active_suppressions */
/* mode: literal */
/* Count active, non-expired suppression entries for an owner.
   Params: owner_id, type
*/
SELECT COUNT(*) AS cnt FROM entries WHERE owner_id = %s AND type = %s AND deleted IS NULL AND (expires IS NULL OR expires > NOW())
