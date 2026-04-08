DELETE FROM session_redirects WHERE expires_at <= NOW();
DELETE FROM session_registry WHERE expires_at <= NOW();
