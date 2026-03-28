-- get_stats: SELECT distinct sources from active entries
SELECT DISTINCT source FROM entries WHERE owner_id = %s AND deleted IS NULL ORDER BY source
