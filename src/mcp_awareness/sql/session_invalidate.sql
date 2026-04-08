UPDATE session_registry
SET expires_at = NOW()
WHERE session_id = %s
