/* name: get_reads */
/* mode: templated */
/* Get read history with dynamic filters and optional limit.
   {{where}} — conditional WHERE clauses built by caller (owner_id, optionally entry_id/timestamp/platform)
   {{limit_clause}} — "LIMIT N" hardcoded by caller (not a bind param)
   Params: owner_id, [entry_id], [since], [platform]
*/
SELECT * FROM reads WHERE {where} ORDER BY timestamp DESC{limit_clause}
