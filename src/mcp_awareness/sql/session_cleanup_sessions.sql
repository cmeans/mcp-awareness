DELETE FROM session_registry WHERE expires_at <= NOW()
