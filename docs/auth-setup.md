# Authentication Setup

Awareness supports two authentication modes that can work independently or together:

- **Self-signed JWT** — generate tokens with the CLI for scripts, edge providers, and programmatic access
- **OAuth 2.1** — external provider (WorkOS, Auth0, Cloudflare Access, Keycloak) for interactive clients like Claude Desktop and Claude Code

Single-user deployments behind a Cloudflare tunnel + WAF don't need either — secret path auth is sufficient. Enable authentication when you need multi-user isolation.

## Quick start

### 1. Choose your auth mode

| Mode | Use case | Setup |
|------|----------|-------|
| Self-signed JWT only | Scripts, edge providers, single admin | Generate secret + token via CLI |
| OAuth only | Interactive AI clients, multi-user | Configure external provider |
| Both (recommended) | Full deployment | OAuth for users, JWT for automation |

### 2. Set environment variables

```bash
# Enable auth (required for both modes)
AWARENESS_AUTH_REQUIRED=true

# Self-signed JWT (optional if using OAuth)
AWARENESS_JWT_SECRET=<your-secret>        # generate with: mcp-awareness-secret
AWARENESS_JWT_ALGORITHM=HS256             # default

# OAuth provider (optional if using self-signed only)
AWARENESS_OAUTH_ISSUER=https://your-provider.example.com
AWARENESS_OAUTH_AUDIENCE=                 # optional — expected aud claim
AWARENESS_OAUTH_JWKS_URI=                 # optional — defaults to {issuer}/.well-known/jwks.json
AWARENESS_OAUTH_USER_CLAIM=sub            # JWT claim used as owner_id
AWARENESS_OAUTH_AUTO_PROVISION=false      # auto-create users on first login

# Owner context
AWARENESS_DEFAULT_OWNER=                  # fallback owner for stdio/unauthenticated (defaults to system username)
```

### 3. Connect your client

**Claude Desktop** — Settings → Connectors → Add custom connector:
- Remote MCP server URL: `https://your-host/mcp`
- OAuth Client ID: from your provider
- OAuth Client Secret: from your provider

**Claude Code** — with pre-configured OAuth credentials:
```bash
claude mcp add --transport http \
  --client-id <client-id> \
  --client-secret <client-secret> \
  awareness https://your-host/mcp
```

**Claude Code** — with self-signed JWT:
```bash
claude mcp add --transport http \
  --header "Authorization: Bearer $(mcp-awareness-token --user alice)" \
  awareness https://your-host/mcp
```

## Environment variables reference

### Authentication

| Variable | Default | Description |
|----------|---------|-------------|
| `AWARENESS_AUTH_REQUIRED` | `false` | Enable authentication on all requests |
| `AWARENESS_JWT_SECRET` | _(required for self-signed)_ | Signing secret for self-signed JWTs |
| `AWARENESS_JWT_ALGORITHM` | `HS256` | JWT signing algorithm |
| `AWARENESS_DEFAULT_OWNER` | _(system username)_ | Default owner_id for stdio and unauthenticated connections |

### OAuth provider

| Variable | Default | Description |
|----------|---------|-------------|
| `AWARENESS_OAUTH_ISSUER` | _(required for OAuth)_ | OIDC issuer URL (e.g., `https://your-domain.authkit.app`) |
| `AWARENESS_OAUTH_AUDIENCE` | _(optional)_ | Expected `aud` claim — validates tokens are intended for this server |
| `AWARENESS_OAUTH_JWKS_URI` | `{issuer}/.well-known/jwks.json` | Override JWKS endpoint for non-standard providers |
| `AWARENESS_OAUTH_USER_CLAIM` | `sub` | JWT claim to use as owner_id (`sub`, `email`, or `preferred_username`) |
| `AWARENESS_OAUTH_AUTO_PROVISION` | `false` | Auto-create user record on first valid OAuth login |

## CLI tools

### `mcp-awareness-secret`

Generate a 256-bit JWT signing secret:

```bash
mcp-awareness-secret
# Output: URL-safe base64 secret — add to .env as AWARENESS_JWT_SECRET
```

### `mcp-awareness-token`

Generate a self-signed JWT for a user:

```bash
mcp-awareness-token --user alice --expires 90d
```

| Flag | Default | Description |
|------|---------|-------------|
| `--user` | _(required)_ | User ID (becomes the `sub` claim / owner_id) |
| `--expires` | `30d` | Token lifetime (`30d`, `24h`, `90d`, etc.) |

Requires `AWARENESS_JWT_SECRET` environment variable.

### `mcp-awareness-user`

User management for multi-tenant deployments:

```bash
# Add a user
mcp-awareness-user add alice --email alice@example.com --display-name "Alice" --timezone "America/Chicago"

# Set password (interactive — validates strength via zxcvbn, min 14 chars)
mcp-awareness-user set-password alice

# List active users
mcp-awareness-user list

# Export user data as JSON (GDPR)
mcp-awareness-user export alice -o alice-data.json

# Delete user and all data (GDPR right to erasure)
mcp-awareness-user delete alice --confirm
```

| Subcommand | Flags |
|------------|-------|
| `add <user_id>` | `--email`, `--display-name`, `--phone` (E.164), `--timezone` (IANA, default: UTC) |
| `set-password <user_id>` | _(interactive prompt)_ |
| `list` | _(none)_ |
| `export <user_id>` | `-o` / `--output` (default: stdout) |
| `delete <user_id>` | `--confirm` (required safety flag) |

Requires `AWARENESS_DATABASE_URL` environment variable.

## Provider setup: WorkOS AuthKit

1. Create a WorkOS account at [workos.com](https://workos.com)
2. In the AuthKit dashboard, enable **CIMD** (Client ID Metadata Documents) and **DCR** (Dynamic Client Registration)
3. Note your AuthKit domain (e.g., `thoughtful-saga-02-staging.authkit.app`)
4. Configure environment:
   ```bash
   AWARENESS_OAUTH_ISSUER=https://your-domain.authkit.app
   ```
5. WorkOS handles login UI, PKCE, and token issuance — no additional server setup needed

Other OIDC-compliant providers (Auth0, Cloudflare Access, Keycloak, AWS Cognito) work the same way — just set `AWARENESS_OAUTH_ISSUER` to your provider's issuer URL.

## User provisioning

### Pre-provision via CLI (recommended for early access)

```bash
mcp-awareness-user add alice --email alice@example.com --display-name "Alice"
```

On first OAuth login, the server matches by email and links the OAuth identity (`oauth_subject` + `oauth_issuer`) to the existing user record.

### Auto-provision on first login

Set `AWARENESS_OAUTH_AUTO_PROVISION=true`. The server creates a user record automatically when a valid OAuth token arrives from an unknown identity.

### Resolution order

When an OAuth token arrives, the server resolves identity in this order:

1. **OAuth lookup** — match by `(oauth_issuer, oauth_subject)` → already-linked user
2. **Email link** — match by email → pre-provisioned user, link OAuth identity on first login
3. **Auto-provision** — create new user (if enabled)
4. **Fallback** — use `sub` claim as owner_id directly (no user record)

## Enabling auth on an existing instance

Alembic migrations handle all schema changes automatically:

1. Set `AWARENESS_AUTH_REQUIRED=true` and your auth env vars in `.env`
2. Restart the server — migrations add `owner_id` columns, `users` table, OAuth columns, and RLS policies
3. Existing data is backfilled to `AWARENESS_DEFAULT_OWNER`
4. Pre-provision users with `mcp-awareness-user add`
5. Generate tokens or configure OAuth provider

## How it works

Awareness is an **OAuth 2.1 resource server** — it validates tokens but doesn't issue them.

- **Self-signed JWTs** are validated against `AWARENESS_JWT_SECRET` (symmetric HS256)
- **OAuth tokens** are validated against the provider's public keys via JWKS (asymmetric RS256/ES256)
- The server tries self-signed first, then falls back to OAuth validation
- `/.well-known/oauth-protected-resource` serves RFC 9728 metadata for client discovery
- 401 responses include `WWW-Authenticate` headers pointing to the OAuth provider
- Owner context propagates via `contextvars` through all store and collator methods
- Postgres RLS policies provide defense-in-depth alongside application-level owner_id filtering

## Known limitations

- **Dual OAuth chain**: Claude Desktop/Code authenticates to Anthropic, then Anthropic's MCP client authenticates to awareness. Either session can expire independently. If tools stop responding mid-conversation, the break may be on Anthropic's side, not yours.
- **Token expiry**: Claude Code's own OAuth tokens expire within 2-4 hours of extended use. This is a known upstream issue, not an awareness bug.
- **Server health**: `/health` is always unauthenticated — use it to verify the server is up independently of auth state.

---

Part of the [<img src="../docs/branding/awareness-logo-32.svg" alt="Awareness logo — a stylized eye with radiating signal lines" height="20"> Awareness](https://github.com/cmeans/mcp-awareness) ecosystem. © 2026 Chris Means
