/* name: link_oauth_identity */
/* mode: literal */
/* Link an OAuth identity to an existing user found by email.
   Sets oauth_subject and oauth_issuer on first OAuth login.
   Only updates if oauth_subject is currently NULL (first-time link).
   Params: oauth_subject, oauth_issuer, email */
UPDATE users
SET oauth_subject = %s, oauth_issuer = %s, updated = now()
WHERE email = %s AND oauth_subject IS NULL AND deleted IS NULL
RETURNING id
