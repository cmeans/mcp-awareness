/* name: get_tags */
/* mode: literal */
/* Get all tags in use with usage counts from active entries.
   Unnests the JSONB tags array and aggregates counts.
   Params: owner_id
*/
SELECT value, COUNT(*) AS cnt FROM entries, jsonb_array_elements_text(tags) AS value WHERE owner_id = %s AND deleted IS NULL GROUP BY value ORDER BY cnt DESC
