-- upsert_status: DELETE existing status before inserting new one
DELETE FROM entries WHERE owner_id = %s AND type = %s AND source = %s AND deleted IS NULL
