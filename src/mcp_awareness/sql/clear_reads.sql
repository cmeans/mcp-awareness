/* name: clear_reads */
/* mode: literal */
/* Delete all reads for a given owner. Params: owner_id */
DELETE FROM reads WHERE owner_id = %s
