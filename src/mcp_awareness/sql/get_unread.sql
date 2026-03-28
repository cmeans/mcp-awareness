/* name: get_unread */
/* mode: templated */
/* Get entries with zero reads, optionally scoped to reads since a timestamp.
   {{since_clause}} — AND r.timestamp >= (since) when provided, else empty
   {{limit_clause}} — LIMIT clause when limit is provided, else empty
   Params: owner_id, [since], [limit]
*/
SELECT e.* FROM entries e LEFT JOIN reads r ON e.id = r.entry_id {since_clause} WHERE e.owner_id = %s AND e.deleted IS NULL AND r.id IS NULL ORDER BY e.created DESC{limit_clause}
