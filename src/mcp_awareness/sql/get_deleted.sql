-- get_deleted: SELECT soft-deleted entries from the trash
SELECT * FROM entries WHERE {where} ORDER BY deleted DESC{limit_clause}
