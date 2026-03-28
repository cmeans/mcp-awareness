/* name: upsert_alert_update */
/* mode: literal */
/* Update an existing alert entry's tags and data during upsert.
   Params: updated, tags (jsonb), data (jsonb), id
*/
UPDATE entries SET updated = %s, tags = %s::jsonb, data = %s::jsonb WHERE id = %s
