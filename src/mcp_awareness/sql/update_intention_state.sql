/* name: update_intention_state */
/* mode: literal */
/* Update an intention entry's data (including state and changelog) and timestamp.
   Python-side computes the state transition and changelog before calling this.
   Params: updated, data (jsonb), id, owner_id
*/
UPDATE entries SET updated = %s, data = %s::jsonb WHERE id = %s AND owner_id = %s
