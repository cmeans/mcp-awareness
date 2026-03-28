-- _query_entries: SELECT entries with dynamic filters
SELECT * FROM entries WHERE owner_id = %s AND deleted IS NULL AND ({where}) ORDER BY {order_by}{limit_clause}
