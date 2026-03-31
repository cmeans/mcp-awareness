/* name: update_user_profile */
UPDATE users
SET email = COALESCE(email, %s),
    canonical_email = COALESCE(canonical_email, %s),
    display_name = COALESCE(display_name, %s),
    updated = now()
WHERE id = %s
  AND (email IS NULL OR display_name IS NULL)
