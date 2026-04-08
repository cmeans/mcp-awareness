/* name: session_cleanup_redirects */
/* mode: literal */
/* Delete all expired redirect mappings.
   Called by the background cleanup thread alongside session_cleanup_sessions.
*/
DELETE FROM session_redirects WHERE expires_at <= NOW()
