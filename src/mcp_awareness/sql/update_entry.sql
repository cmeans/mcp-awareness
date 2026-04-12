/* name: update_entry */
/* mode: literal */
/* Update a knowledge entry's mutable fields (source, tags, data with changelog, language).
   Used for note, pattern, context, preference types only — status/alert/suppression
   are immutable. Python-side computes the changelog diff before calling this.
   Params: updated, source, tags (jsonb), data (jsonb), language (regconfig), id, owner_id
*/
UPDATE entries SET updated = %s, source = %s, tags = %s::jsonb, data = %s::jsonb, language = %s::regconfig WHERE id = %s AND owner_id = %s
