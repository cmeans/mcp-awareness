UPDATE session_registry
SET last_seen = NOW(), expires_at = NOW() + make_interval(secs => %s)
WHERE session_id = %s AND expires_at > NOW()
