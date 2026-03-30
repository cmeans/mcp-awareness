/* name: get_user_by_oauth */
/* mode: literal */
/* Look up a user by OAuth identity (issuer + subject). Params: oauth_issuer, oauth_subject */
SELECT id, email, display_name, timezone, oauth_subject, oauth_issuer, created
FROM users
WHERE oauth_issuer = %s AND oauth_subject = %s AND deleted IS NULL
