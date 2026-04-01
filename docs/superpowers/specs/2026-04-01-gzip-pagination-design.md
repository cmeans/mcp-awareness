# Design: Gzip Compression + Pagination Improvements (#112)

## Summary

Two production-readiness improvements: gzip response compression to reduce egress bandwidth, and pagination hardening with accurate `has_more` signaling.

## Gzip Compression

Add Starlette's `GZipMiddleware` as the outermost ASGI middleware layer, wrapping all other middleware (SecretPath/Health, WellKnown, Auth). Compresses responses over 500 bytes when the client sends `Accept-Encoding: gzip`. No new dependencies — Starlette ships it.

**Middleware order (outermost → innermost):**
1. GZipMiddleware (new)
2. SecretPathMiddleware or HealthMiddleware
3. WellKnownMiddleware
4. AuthMiddleware
5. FastMCP app

Only applies to HTTP transport. stdio transport is unaffected.

## Pagination Changes

### Default limit reduction

`DEFAULT_QUERY_LIMIT` in `helpers.py` changes from 200 to 100. This is the fallback when callers omit the `limit` parameter.

### Accurate `has_more` via limit+1 pattern

All paginated tools fetch `limit + 1` rows from the store, return at most `limit`, and set `has_more: true` if the extra row existed. No count query needed.

### Response shape

Paginated tool responses include metadata alongside the data:

```json
{
  "entries": [...],
  "limit": 100,
  "offset": 0,
  "has_more": true
}
```

When `has_more` is true, the agent can request the next page with `offset=100`.

### Affected tools

| Tool | Current default | New default | Notes |
|------|----------------|-------------|-------|
| `get_alerts` | 200 | 100 | limit + offset |
| `get_knowledge` | 200 | 100 | limit + offset |
| `get_deleted` | 200 | 100 | limit + offset |
| `get_reads` | 200 | 100 | limit only |
| `get_actions` | 200 | 100 | limit only |
| `get_unread` | 200 | 100 | limit only |
| `get_activity` | 200 | 100 | limit only |
| `get_intentions` | 200 | 100 | limit only |
| `semantic_search` | 10 (max 100) | unchanged | gets `has_more` treatment |

### What doesn't change

- `_validate_pagination` still clamps and applies defaults
- SQL-level LIMIT/OFFSET mechanics unchanged (just passes `limit + 1`)
- `semantic_search` keeps its own default of 10 and max of 100

## Implementation approach

### Gzip
- Import `GZipMiddleware` from `starlette.middleware.gzip`
- Wrap the app in `server.py` at both transport paths (with and without mount path)

### Pagination
- Update `DEFAULT_QUERY_LIMIT` in `helpers.py`
- Add a shared helper to apply the limit+1 pattern and build the pagination metadata dict
- Update each paginated tool to use the helper and include metadata in the response

## Testing

### Gzip
- Response includes `Content-Encoding: gzip` when client sends `Accept-Encoding: gzip` and response exceeds 500 bytes
- Response is uncompressed when client doesn't request it
- Small responses (< 500 bytes) are not compressed

### Pagination
- `has_more: true` when more data exists beyond the limit
- `has_more: false` when all data fits within the limit
- Default limit is 100
- Pagination metadata (`limit`, `offset`, `has_more`) present in all paginated responses
- Existing offset pagination still works correctly with the new metadata
