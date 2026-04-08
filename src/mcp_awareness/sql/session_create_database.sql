-- Create the session database with explicit UTF-8 encoding.
-- Uses template0 to avoid inheriting SQL_ASCII from template1.
-- Locale defaults to template0's locale (C); specifying LC_COLLATE/LC_CTYPE
-- is omitted for portability — not all environments have C.UTF-8 or en_US.UTF-8.
-- The {} placeholder is formatted via psycopg.sql.Identifier.
CREATE DATABASE {} ENCODING 'UTF8' TEMPLATE template0
