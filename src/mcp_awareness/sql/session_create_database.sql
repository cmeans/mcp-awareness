-- Create the session database with explicit UTF-8 encoding.
-- Uses template0 to avoid inheriting SQL_ASCII from template1.
-- LC_COLLATE/LC_CTYPE 'C' is universally portable (C.UTF-8 vs C.utf8
-- varies by OS). The session DB stores IDs and timestamps, not
-- locale-sensitive text, so byte-order collation is fine.
-- The database name placeholder is formatted via psycopg.sql.Identifier.
CREATE DATABASE {} ENCODING 'UTF8' LC_COLLATE 'C' LC_CTYPE 'C' TEMPLATE template0
