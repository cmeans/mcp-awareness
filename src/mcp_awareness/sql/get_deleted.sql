/* name: get_deleted */
/* mode: templated */
/* Get soft-deleted entries from the trash with optional filters and pagination.
   {{where}} — conditional WHERE clauses built by caller (owner_id, deleted IS NOT NULL, optionally since)
   {{limit_clause}} — LIMIT and/or OFFSET clause appended when provided
   Params: owner_id, [since], [limit], [offset]
*/
SELECT * FROM entries WHERE {where} ORDER BY deleted DESC{limit_clause}
