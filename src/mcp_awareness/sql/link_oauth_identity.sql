/* name: link_oauth_identity */
/* mode: literal */
/* Link an OAuth identity to an existing user found by canonical email.
   Sets oauth_subject and oauth_issuer on first OAuth login.
   Only updates if oauth_subject is currently NULL (first-time link).
   Uses canonical_email for matching (handles Gmail dot/+tag variants).
   Params: oauth_subject, oauth_issuer, canonical_email */
UPDATE users
SET oauth_subject = %s, oauth_issuer = %s, updated = now()
WHERE canonical_email = %s AND oauth_subject IS NULL AND deleted IS NULL
RETURNING id
