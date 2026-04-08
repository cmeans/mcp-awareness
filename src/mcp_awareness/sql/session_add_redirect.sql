INSERT INTO session_redirects (old_session_id, new_session_id, created_at, expires_at)
VALUES (%s, %s, NOW(), NOW() + make_interval(secs => %s))
ON CONFLICT (old_session_id) DO UPDATE
SET new_session_id = EXCLUDED.new_session_id,
    created_at = EXCLUDED.created_at,
    expires_at = EXCLUDED.expires_at
