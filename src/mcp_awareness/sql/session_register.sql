INSERT INTO session_registry
    (session_id, owner_id, node, protocol_version, capabilities, client_info, created_at, last_seen, expires_at)
VALUES
    (%s, %s, %s, %s, %s, %s, NOW(), NOW(), NOW() + make_interval(secs => %s))
