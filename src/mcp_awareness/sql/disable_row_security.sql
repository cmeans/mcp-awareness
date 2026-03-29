/* name: disable_row_security */
/* mode: literal */
/* Disable RLS for the current transaction only (SET LOCAL).
   Used by system-wide maintenance tasks that must operate across all owners. */
SET LOCAL row_security = off
