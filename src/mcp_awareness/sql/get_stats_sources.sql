/* name: get_stats_sources */
/* mode: literal */
/* Get distinct sources from all active entries for statistics.
   Params: owner_id
*/
SELECT DISTINCT source FROM entries WHERE owner_id = %s AND deleted IS NULL ORDER BY source
