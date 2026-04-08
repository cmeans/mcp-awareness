/* name: session_lookup */
/* mode: literal */
/* Look up an active session by session_id. Returns nothing if expired.
   Params: session_id
*/
SELECT session_id, owner_id, node, protocol_version, capabilities, client_info,
       created_at, last_seen, expires_at
FROM session_registry
WHERE session_id = %s AND expires_at > NOW()
