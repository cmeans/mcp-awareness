/* name: get_actions */
/* mode: templated */
/* Get action history with dynamic filters and optional limit.
   {{where}} — conditional WHERE clauses built by caller (owner_id, optionally entry_id/timestamp/platform/tags)
   {{limit_clause}} — "LIMIT ?" with bind param, or empty string
   Params: owner_id, [entry_id], [since], [platform], [...tag jsonb values]
*/
SELECT * FROM actions WHERE {where} ORDER BY timestamp DESC{limit_clause}
