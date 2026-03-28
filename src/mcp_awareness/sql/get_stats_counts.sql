-- get_stats: SELECT entry counts grouped by type
SELECT type, COUNT(*) AS cnt FROM entries WHERE owner_id = %s AND deleted IS NULL GROUP BY type
