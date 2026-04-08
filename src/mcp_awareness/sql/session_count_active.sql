/* name: session_count_active */
/* mode: literal */
/* Count active (non-expired) sessions for a given owner.
   Params: owner_id
*/
SELECT COUNT(*) AS cnt
FROM session_registry
WHERE owner_id = %s AND expires_at > NOW()
