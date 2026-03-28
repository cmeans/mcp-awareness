-- get_unread: SELECT entries with zero reads
SELECT e.* FROM entries e LEFT JOIN reads r ON e.id = r.entry_id {since_clause} WHERE e.owner_id = %s AND e.deleted IS NULL AND r.id IS NULL ORDER BY e.created DESC{limit_clause}
