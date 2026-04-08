SELECT session_id, owner_id, node, protocol_version, capabilities, client_info,
       created_at, last_seen, expires_at
FROM session_registry
WHERE session_id = %s AND expires_at > NOW()
