-- _do_cleanup: DELETE entries past their expiration
DELETE FROM entries WHERE expires IS NOT NULL AND expires <= %s
