/* name: log_action */
/* mode: literal */
/* Insert an action-tracking record and return its generated ID.
   Params: owner_id, entry_id, timestamp, platform, action, detail, tags (jsonb)
*/
INSERT INTO actions (owner_id, entry_id, timestamp, platform, action, detail, tags) VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb) RETURNING id
