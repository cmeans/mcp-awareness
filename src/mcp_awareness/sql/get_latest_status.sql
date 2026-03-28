-- get_latest_status: SELECT the most recent active status for a source
SELECT * FROM entries WHERE owner_id = %s AND type = %s AND source = %s AND deleted IS NULL ORDER BY created DESC LIMIT 1
