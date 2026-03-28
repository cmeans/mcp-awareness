-- get_read_counts: SELECT read count and last read time per entry
SELECT entry_id, COUNT(*) AS cnt, MAX(timestamp) AS last FROM reads WHERE owner_id = %s AND entry_id IN ({placeholders}) GROUP BY entry_id
