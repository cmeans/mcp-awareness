-- Create the session database with explicit UTF-8 encoding.
-- Uses template0 to avoid inheriting SQL_ASCII from template1.
-- C.UTF-8 is portable across all Linux environments (unlike en_US.UTF-8).
-- The {} placeholder is formatted via psycopg.sql.Identifier.
CREATE DATABASE {} ENCODING 'UTF8' LC_COLLATE 'C.UTF-8' LC_CTYPE 'C.UTF-8' TEMPLATE template0
