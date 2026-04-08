/* name: session_delete_redirects_to */
/* mode: literal */
/* Delete all redirect mappings pointing to a given new_session_id.
   Called when a session is invalidated to clean up any inbound redirects.
   Params: new_session_id
*/
DELETE FROM session_redirects WHERE new_session_id = %s
