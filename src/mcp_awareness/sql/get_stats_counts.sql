/* name: get_stats_counts */
/* mode: literal */
/* Get entry counts grouped by type for statistics.
   Params: owner_id
*/
SELECT type, COUNT(*) AS cnt FROM entries WHERE owner_id = %s AND deleted IS NULL GROUP BY type
