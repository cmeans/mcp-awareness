SELECT COUNT(*) AS cnt
FROM session_registry
WHERE owner_id = %s AND expires_at > NOW()
