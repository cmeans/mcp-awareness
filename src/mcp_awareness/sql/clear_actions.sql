/* name: clear_actions */
/* mode: literal */
/* Delete all actions for a given owner. Params: owner_id */
DELETE FROM actions WHERE owner_id = %s
