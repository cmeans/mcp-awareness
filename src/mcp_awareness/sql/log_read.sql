-- log_read: INSERT a read-tracking record
INSERT INTO reads (owner_id, entry_id, platform, tool_used) VALUES (%s, %s, %s, %s)
