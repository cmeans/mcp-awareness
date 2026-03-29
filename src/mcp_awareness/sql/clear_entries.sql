/* name: clear_entries */
/* mode: literal */
/* Delete all entries for a given owner. Params: owner_id */
DELETE FROM entries WHERE owner_id = %s
