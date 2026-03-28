-- count_active_suppressions: COUNT active, non-expired suppression entries
SELECT COUNT(*) AS cnt FROM entries WHERE owner_id = %s AND type = %s AND deleted IS NULL AND (expires IS NULL OR expires > NOW())
