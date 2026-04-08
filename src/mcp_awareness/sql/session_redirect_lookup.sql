/* name: session_redirect_lookup */
/* mode: literal */
/* Look up the new session ID for a given old session ID, if the redirect
   mapping exists and has not expired.
   Params: old_session_id
*/
SELECT new_session_id
FROM session_redirects
WHERE old_session_id = %s AND expires_at > NOW()
