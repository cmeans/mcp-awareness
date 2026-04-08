SELECT new_session_id
FROM session_redirects
WHERE old_session_id = %s AND expires_at > NOW()
