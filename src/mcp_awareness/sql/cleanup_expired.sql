/* name: cleanup_expired */
/* mode: literal */
/* Delete entries past their expiration timestamp. Runs on a background
   daemon thread, debounced to 10-second intervals. Handles both natural
   expiry and trash retention (soft-deleted entries expire after 30 days).
   Params: now (current UTC timestamp)
*/
DELETE FROM entries WHERE expires IS NOT NULL AND expires <= %s
