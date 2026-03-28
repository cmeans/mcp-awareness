-- get_sources: SELECT distinct sources with active status entries
SELECT DISTINCT source FROM entries WHERE owner_id = %s AND type = %s AND deleted IS NULL
