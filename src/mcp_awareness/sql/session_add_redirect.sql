/* name: session_add_redirect */
/* mode: literal */
/* Upsert a redirect mapping from old_session_id to new_session_id.
   Called after cross-node re-initialization so the old client session ID
   routes to the newly registered session.
   Params: old_session_id, new_session_id, ttl_seconds
*/
INSERT INTO session_redirects (old_session_id, new_session_id, created_at, expires_at)
VALUES (%s, %s, NOW(), NOW() + make_interval(secs => %s))
ON CONFLICT (old_session_id) DO UPDATE
SET new_session_id = EXCLUDED.new_session_id,
    created_at = EXCLUDED.created_at,
    expires_at = EXCLUDED.expires_at
