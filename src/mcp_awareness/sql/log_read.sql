/* name: log_read */
/* mode: literal */
/* Insert a read-tracking record. Called once per entry_id in a batch.
   Params: owner_id, entry_id, platform, tool_used
*/
INSERT INTO reads (owner_id, entry_id, platform, tool_used) VALUES (%s, %s, %s, %s)
