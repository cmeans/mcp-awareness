-- get_actions: SELECT action history with dynamic filters
SELECT * FROM actions WHERE {where} ORDER BY timestamp DESC{limit_clause}
