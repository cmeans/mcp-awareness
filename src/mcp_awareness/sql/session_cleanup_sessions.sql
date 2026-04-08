/* name: session_cleanup_sessions */
/* mode: literal */
/* Delete all expired sessions from session_registry.
   Called by the background cleanup thread. Expired sessions have
   expires_at <= NOW() after TTL-based sliding expiry elapses.
*/
DELETE FROM session_registry WHERE expires_at <= NOW()
