-- Create the session database with explicit UTF-8 encoding.
-- Uses template0 to avoid inheriting SQL_ASCII from template1.
-- The {} placeholder is formatted via psycopg.sql.Identifier.
CREATE DATABASE {} ENCODING 'UTF8' LC_COLLATE 'en_US.UTF-8' LC_CTYPE 'en_US.UTF-8' TEMPLATE template0
