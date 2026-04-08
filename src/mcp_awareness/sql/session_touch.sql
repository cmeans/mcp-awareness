/* name: session_touch */
/* mode: literal */
/* Extend the TTL of an active session (sliding expiry).
   Only updates non-expired sessions. Touch is debounced by the caller.
   Params: ttl_seconds, session_id
*/
UPDATE session_registry
SET last_seen = NOW(), expires_at = NOW() + make_interval(secs => %s)
WHERE session_id = %s AND expires_at > NOW()
