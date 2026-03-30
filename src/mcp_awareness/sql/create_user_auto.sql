/* name: create_user_auto */
/* mode: literal */
/* Auto-provision a user on first OAuth login. No-op if user already exists.
   Handles both id and canonical_email uniqueness conflicts.
   Params: user_id, email, canonical_email, display_name, oauth_subject, oauth_issuer */
INSERT INTO users (id, email, canonical_email, display_name, oauth_subject, oauth_issuer, created)
VALUES (%s, %s, %s, %s, %s, %s, now())
ON CONFLICT DO NOTHING
