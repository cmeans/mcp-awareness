/* name: _query_entries */
/* mode: templated */
/* Select entries with dynamic owner, filter, ordering, and pagination.
   {{where}} — conditional WHERE clauses built by caller from optional filters
   {{order_by}} — ORDER BY clause, default "updated DESC"
   {{limit_clause}} — LIMIT and/or OFFSET clause appended when provided
   Params: owner_id, ...filter params from {{where}}, [limit], [offset]
*/
SELECT * FROM entries WHERE owner_id = %s AND deleted IS NULL AND ({where}) ORDER BY {order_by}{limit_clause}
