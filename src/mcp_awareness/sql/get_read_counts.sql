/* name: get_read_counts */
/* mode: templated */
/* Get read count and last read timestamp per entry for list mode enrichment.
   {{placeholders}} — comma-separated bind params for IN clause, one per entry_id
   Params: owner_id, ...entry_ids
*/
SELECT entry_id, COUNT(*) AS cnt, MAX(timestamp) AS last FROM reads WHERE owner_id = %s AND entry_id IN ({placeholders}) GROUP BY entry_id
