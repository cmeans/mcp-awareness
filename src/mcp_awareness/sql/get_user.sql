/* name: get_user */
/* mode: literal */
/* Get a user by ID. Params: user_id */
SELECT id, email, display_name, timezone, created FROM users WHERE id = %s AND deleted IS NULL
