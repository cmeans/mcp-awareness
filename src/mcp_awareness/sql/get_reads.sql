-- get_reads: SELECT read history with dynamic filters
SELECT * FROM reads WHERE {where} ORDER BY timestamp DESC{limit_clause}
