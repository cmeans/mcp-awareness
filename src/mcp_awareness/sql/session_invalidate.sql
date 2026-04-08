/* name: session_invalidate */
/* mode: literal */
/* Immediately expire a session by setting expires_at to NOW().
   Used when a client sends DELETE /mcp or during forced invalidation.
   Params: session_id
*/
UPDATE session_registry
SET expires_at = NOW()
WHERE session_id = %s
