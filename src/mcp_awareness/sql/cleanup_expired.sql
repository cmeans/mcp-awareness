/* name: cleanup_expired */
/* mode: literal */
/* Delete expired entries for a specific owner who has opted in to auto-cleanup.
   RLS-safe — scoped by owner_id, no row_security bypass needed.
   Params: now (current UTC timestamp), owner_id
*/
DELETE FROM entries WHERE expires IS NOT NULL AND expires <= %s AND owner_id = %s
