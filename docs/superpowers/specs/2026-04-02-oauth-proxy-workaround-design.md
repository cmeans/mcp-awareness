<!-- SPDX-License-Identifier: AGPL-3.0-or-later | Copyright (C) 2026 Chris Means -->
# OAuth Proxy Workaround — Design Spec

**Date**: 2026-04-02
**Status**: Draft
**Author**: Chris Means + Claude Code

## Problem

Two bugs in Claude Desktop / Claude.ai's MCP OAuth implementation prevent authentication with MCP servers that delegate to external identity providers:

- **Bug A — "Token never sent"**: OAuth flow completes, `POST /token` returns 200, but Claude never attaches the Bearer token to subsequent MCP requests. Open since Dec 2025. Claude Code is unaffected.
- **Bug B — "Ignores external auth endpoints"**: Claude.ai fetches `/.well-known/oauth-authorization-server` but ignores the `authorization_endpoint` and `token_endpoint` values, instead constructing URLs from the MCP server's base URL (e.g., `https://mcpawareness.com/authorize` instead of `https://thoughtful-saga-02-staging.authkit.app/authorize`).

Bug B is the primary blocker for awareness — WorkOS endpoints are never reached. No public fix ETA from Anthropic as of 2026-04-02.

**Tracked issues**: modelcontextprotocol/servers #5, #46, #62, #75, #79, #82, #125.

## Solution

Add a thin OAuth proxy middleware that intercepts the routes Claude.ai incorrectly constructs on the awareness domain and forwards them to the real WorkOS endpoints. The proxy is feature-gated, fully isolated in its own module, and designed for removal once the upstream bugs are fixed.

## Architecture

### New Module: `src/mcp_awareness/oauth_proxy.py`

A single ASGI middleware class (`OAuthProxyMiddleware`) that:

1. Discovers real OAuth endpoints from the issuer's OIDC configuration at init
2. Intercepts `/authorize`, `/token`, `/register` requests
3. Forwards them to WorkOS
4. Passes all other requests through unchanged

### Feature Gate

**Env var**: `AWARENESS_OAUTH_PROXY` (default: `false`)

When `false`, the middleware is not added to the ASGI chain — zero overhead.

### Middleware Placement

The proxy slots in outside `AuthMiddleware` since the intercepted routes are part of the OAuth flow (pre-authentication):

```
GZip → OAuthProxyMiddleware → AuthMiddleware → WellKnown → Health → MCP app
```

### Target Configuration: No Secret Mount Path

The primary deployment target uses no `MOUNT_PATH`. OAuth routes live at the public root (`/authorize`, `/token`, `/register`) — exactly where Claude.ai expects them. Cloudflare WAF handles abuse at the edge (see Cloudflare WAF section below).

For deployments with `MOUNT_PATH`: the proxy should be placed inside the `SecretPathMiddleware` rewrite so it sees clean paths. This is not the primary configuration and is noted for completeness.

## Route Handling

### `GET /authorize` — Redirect

Claude.ai sends the user's browser here with OAuth query params.

**Handling**: Read all query parameters, append them to the real `authorization_endpoint` discovered from OIDC config, return a **302 Found** redirect.

```
GET /authorize?client_id=X&redirect_uri=Y&state=Z&...
→ 302 Location: https://thoughtful-saga-02-staging.authkit.app/authorize?client_id=X&redirect_uri=Y&state=Z&...
```

No outbound HTTP call — just URL rewriting.

### `POST /token` — Full Proxy

Claude.ai exchanges the auth code for tokens by POSTing here.

**Handling**: Forward the request body and `Content-Type` header to the real `token_endpoint` via `urllib.request`. Relay back the response body, status code, `Content-Type`, `Cache-Control`, and `Set-Cookie` headers.

Must be a true proxy (not a redirect) because POST redirects change method semantics (browsers convert POST→GET on 302) and the response body contains the access token.

### `POST /register` — Full Proxy (Optional)

Dynamic Client Registration (RFC 7591). Same proxying pattern as `/token`.

If the OIDC config doesn't expose a `registration_endpoint`, return **404**. Claude.ai handles this gracefully (clients must be pre-registered in WorkOS).

### `OPTIONS /token`, `OPTIONS /register` — CORS Preflight

Claude.ai runs in a browser. Cross-origin POST requests trigger preflight.

**Handling**: Respond with:
- `Access-Control-Allow-Origin: *` (permissive — these are public OAuth endpoints; tightening to specific origins would break clients)
- `Access-Control-Allow-Methods: POST, OPTIONS`
- `Access-Control-Allow-Headers: Content-Type, Authorization`
- `Access-Control-Max-Age: 86400`

Without this, the browser silently blocks the request — no error, no traffic, hours of debugging.

### Common Behavior

- **No auth required**: All routes are pre-authentication
- **No body modification**: Transparent passthrough — the proxy does not inspect, validate, or alter request/response payloads
- **No token logging**: Token values are never logged or stored
- **Timeout**: 15-second timeout on upstream requests. Return **502** on timeout
- **Error relay**: Upstream 4xx/5xx responses are relayed as-is — the error is from WorkOS and Claude.ai needs to see it
- **Response header forwarding**: Relay `Content-Type`, `Cache-Control`, `Set-Cookie`, and `WWW-Authenticate` from upstream responses

## Endpoint Discovery

At init, the proxy fetches `{OAUTH_ISSUER}/.well-known/openid-configuration` and extracts:

- `authorization_endpoint` → target for `/authorize` redirect
- `token_endpoint` → target for `/token` proxy
- `registration_endpoint` → target for `/register` proxy (optional)

This reuses the same OIDC discovery pattern as `OAuthTokenValidator._discover_oidc_config()`. Endpoints are **pinned at startup** and never re-discovered from client input (SSRF prevention).

If discovery fails, the proxy logs an error and disables itself (passthrough mode) rather than failing the entire server.

## Security

### Rate Limiting

Per-IP rate limits on proxy routes using a sliding window:

| Route | Limit |
|-------|-------|
| `/authorize` | 20 requests/minute per IP |
| `/token` | 10 requests/minute per IP |
| `/register` | 5 requests/minute per IP |

When exceeded, return **429 Too Many Requests** with `Retry-After` header. Log at WARNING with source IP.

Implementation: lightweight `_RateLimiter` class inside `oauth_proxy.py`. Dictionary of `{ip: [timestamps]}`, pruned on each check. Entries evict after the window expires to prevent unbounded memory growth.

### Auto-Ban on Bogus Requests

Certain request patterns are unambiguously malicious:

- **Missing required OAuth params**: `/authorize` without `response_type`, `client_id`, or `redirect_uri`
- **Injection attempts**: SQL/path traversal patterns in param values (`../`, `'; DROP`, etc.)
- **Wrong HTTP method**: `POST /authorize` or `GET /token`

On detection:
- **Temporary ban**: Block the IP for 1 hour
- **Configurable**: `AWARENESS_OAUTH_PROXY_BAN_DURATION` env var (seconds, default: `3600`)
- **Log at WARNING** with reason and source IP
- **Not permanent**: Bans expire. Permanent blocks are Cloudflare WAF's responsibility

### IP Resolution

The proxy identifies clients using a **configurable header priority chain** rather than hardcoding any provider-specific header.

**Env var**: `AWARENESS_OAUTH_PROXY_IP_HEADERS` (comma-separated, ordered by trust)

**Default**: `CF-Connecting-IP,X-Real-IP`

Resolution order:
1. Walk the configured headers in order, return the first non-empty value
2. Fall back to ASGI `client` address (direct connections, local dev)

**Why configurable**: The current deployment uses Cloudflare, which sets `CF-Connecting-IP` (trustworthy — set by Cloudflare's edge, cannot be spoofed by clients). If the infrastructure moves to AWS, the header changes (e.g., `X-Amzn-Source-Ip` or ALB-appended `X-Forwarded-For`). Making this configurable avoids a code change on infra migration.

**Startup log**: `OAuth proxy: IP resolution chain = ['CF-Connecting-IP', 'X-Real-IP', 'asgi-client']`

**Runtime warning**: If none of the configured headers are found in the first N requests, log at WARNING — signal that the config needs updating after an infra change.

> **Infrastructure dependency**: IP identification relies on trusted proxy headers. When changing infrastructure (AWS, GCP, bare metal), update `AWARENESS_OAUTH_PROXY_IP_HEADERS` to match your load balancer's client-IP header. Without a trusted header, rate limiting and bans fall back to the immediate connection IP, which may be a NAT gateway shared by all clients.

### Threat Summary

| Threat | Risk | Mitigation |
|--------|------|------------|
| Open redirect | Low | Target URL pinned at startup; WorkOS validates params |
| SSRF | None | Endpoints from OIDC config, not client input |
| DDoS / amplification | Moderate | Rate limiting + auto-ban + Cloudflare WAF |
| Token exposure | No change | Tokens already transit infra; not logged |
| Credential stuffing | N/A | No credentials stored; WorkOS handles auth |

## Traffic Monitoring & Bug-Fix Detection

### Health Endpoint

The proxy exposes stats via the existing `/health` endpoint:

```json
{
  "status": "ok",
  "oauth_proxy": {
    "enabled": true,
    "completed_flows": 8,
    "last_completed_flow": "2026-04-02T13:45:00Z",
    "raw_hits": {"authorize": 15, "token": 12, "register": 3},
    "rate_limited": {"authorize": 0, "token": 2, "register": 1},
    "last_rate_limited": "2026-04-02T14:01:00Z",
    "banned_ips": 1,
    "last_ban": "2026-04-02T14:02:00Z"
  }
}
```

A "completed flow" = a `/token` request where WorkOS returned **200** with a response containing `access_token`.

### Detecting the Upstream Fix

When Claude.ai fixes Bug B, it will follow the real `authorization_endpoint` and `token_endpoint` from `/.well-known/oauth-authorization-server` directly to WorkOS. The proxy routes go silent.

**Passive detection**: Watch `last_completed_flow`. If it goes stale for days/weeks while Claude.ai users are actively connecting, the bug is fixed.

**Active detection**: After a Claude Desktop update, check the health endpoint. If `completed_flows` stops incrementing while auth still works, disable the proxy (`AWARENESS_OAUTH_PROXY=false`) and confirm clients still authenticate.

**Distinguishing real traffic from probes**: `completed_flows` tracks actual successful token exchanges with WorkOS. Random scanners generate `raw_hits` but not `completed_flows`. If `raw_hits` climbs but `completed_flows` is flat, that's probe traffic.

### Important Caveat: Bug A

The proxy fixes **Bug B** (ignores external endpoints). If **Bug A** ("token never sent") is also present in Claude.ai, the proxy will show `completed_flows` incrementing but users still won't be able to connect — because Claude.ai gets the token but never attaches it.

Diagnosis: if `completed_flows` climbs but no authenticated MCP requests arrive at the server, Bug A is the remaining blocker and requires an upstream fix from Anthropic.

## Cloudflare WAF Configuration

Without the secret mount path, the MCP endpoint is publicly accessible. Cloudflare WAF provides edge-level protection.

### Recommended WAF Rules

Configure these in the Cloudflare dashboard under **Security → WAF → Custom rules** for the `mcpawareness.com` zone:

#### Rule 1: Rate Limit MCP Endpoint

- **Name**: `rate-limit-mcp`
- **When**: `URI Path equals /mcp`
- **Then**: Rate limit
- **Rate**: 60 requests per minute per IP
- **Action on exceed**: Block for 60 seconds
- **With response type**: Default Cloudflare block page

#### Rule 2: Rate Limit OAuth Proxy Routes

- **Name**: `rate-limit-oauth-proxy`
- **When**: `URI Path is in {/authorize, /token, /register}`
- **Then**: Rate limit
- **Rate**: 30 requests per minute per IP
- **Action on exceed**: Block for 300 seconds (5 minutes)
- **With response type**: Default Cloudflare block page

This is a coarser outer limit — the application-level rate limiter has tighter per-route limits but Cloudflare catches volumetric abuse before it reaches the server.

#### Rule 3: Block Non-MCP Paths

- **Name**: `block-unknown-paths`
- **When**: `URI Path` does not match any of: `/mcp`, `/health`, `/authorize`, `/token`, `/register`, `/favicon.ico`, `/.well-known/*`
- **Then**: Block
- **With response type**: Default Cloudflare block page

Prevents scanning of paths that don't exist on the server.

#### Rule 4: Bot Management (Optional)

- **Name**: `challenge-suspicious-bots`
- **When**: `Bot Score less than 30` AND `URI Path equals /mcp`
- **Then**: Managed Challenge
- **Note**: Requires Cloudflare Pro or higher. May interfere with legitimate MCP clients — test before enabling. Claude.ai and Claude Desktop should pass bot challenges, but verify.

### Setup Steps

1. Log in to [Cloudflare Dashboard](https://dash.cloudflare.com)
2. Select the `mcpawareness.com` zone
3. Navigate to **Security → WAF → Custom rules**
4. Click **Create rule** for each rule above
5. Set rule priority: Rule 3 (block unknown) first, then Rule 1 and 2 (rate limits), then Rule 4 (bot) last
6. **Test before enforcing**: Set each rule to "Log" mode first, monitor for a day, then switch to "Block"

### Monitoring

- **Security → Overview**: Shows blocked requests, challenged requests, rate-limited requests
- **Security → Events**: Per-request detail — IP, path, rule matched, action taken
- Set up a **Notification** (Account Home → Notifications) for WAF alerts if blocked requests spike

## Environment Variables Summary

| Variable | Default | Description |
|----------|---------|-------------|
| `AWARENESS_OAUTH_PROXY` | `false` | Enable OAuth proxy routes |
| `AWARENESS_OAUTH_PROXY_BAN_DURATION` | `3600` | Auto-ban duration in seconds |
| `AWARENESS_OAUTH_PROXY_IP_HEADERS` | `CF-Connecting-IP,X-Real-IP` | Trusted IP header priority chain |

All existing OAuth env vars (`AWARENESS_OAUTH_ISSUER`, etc.) are reused — no new provider configuration needed.

## Module Boundaries

- **`oauth_proxy.py`** (new): ASGI middleware, rate limiter, IP resolution, OIDC endpoint discovery, health stats
- **`middleware.py`**: Unchanged — no modifications to existing middleware classes
- **`oauth.py`**: Unchanged — token validation is a separate concern from the proxy
- **`server.py`**: Minor changes to `_run()` — add `OAuthProxyMiddleware` to the ASGI chain when `AWARENESS_OAUTH_PROXY=true`, pass proxy stats to health builder

## Testing

- Unit tests for `_RateLimiter` (window expiry, per-IP isolation, ban logic)
- Unit tests for IP resolution (header priority, fallback behavior)
- Unit tests for each route handler (redirect params, proxy forwarding, CORS preflight, error relay)
- Unit tests for bogus request detection (missing params, injection patterns, wrong method)
- Integration test with mocked WorkOS OIDC config endpoint
- Health endpoint includes proxy stats when enabled, omits when disabled

## Removal Plan

When the upstream bugs are fixed:

1. Confirm `last_completed_flow` is stale while users authenticate successfully
2. Set `AWARENESS_OAUTH_PROXY=false` in environment
3. Monitor for a release cycle — confirm no auth regressions
4. Remove `oauth_proxy.py`, its tests, and the `_run()` wiring
5. Remove the three `AWARENESS_OAUTH_PROXY_*` env vars from documentation
6. Remove Cloudflare WAF Rule 2 (OAuth proxy rate limit)
