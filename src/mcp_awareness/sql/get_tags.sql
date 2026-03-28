-- get_tags: SELECT all tags with usage counts from active entries
SELECT value, COUNT(*) AS cnt FROM entries, jsonb_array_elements_text(tags) AS value WHERE owner_id = %s AND deleted IS NULL GROUP BY value ORDER BY cnt DESC
