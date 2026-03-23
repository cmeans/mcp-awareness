#!/usr/bin/env bash
set -euo pipefail

# ─────────────────────────────────────────────────────────────────────────────
# mcp-awareness demo installer
#
# Downloads and starts mcp-awareness with a Cloudflare quick tunnel.
# You'll get a public URL you can use from any MCP client — Claude.ai,
# Claude Desktop, Claude Code, Cursor, or anything else.
#
# The quick tunnel URL is ephemeral — it changes every time you restart.
# For a stable URL, see the Deployment Guide in the repo.
#
# Review this script before running it:
#   https://github.com/cmeans/mcp-awareness/blob/main/install.sh
# ─────────────────────────────────────────────────────────────────────────────

# TODO: change back to main before merging
COMPOSE_URL="https://raw.githubusercontent.com/cmeans/mcp-awareness/one-click-install/docker-compose.demo.yaml"
DEFAULT_DIR="$HOME/mcp-awareness"
INSTALL_DIR=""

# ── Colors ───────────────────────────────────────────────────────────────────

if [ -t 1 ]; then
  GREEN='\033[0;32m'
  RED='\033[0;31m'
  YELLOW='\033[0;33m'
  BLUE='\033[0;34m'
  BOLD='\033[1m'
  RESET='\033[0m'
else
  GREEN='' RED='' YELLOW='' BLUE='' BOLD='' RESET=''
fi

ok()   { printf "${GREEN}✓${RESET} %s\n" "$1"; }
fail() { printf "${RED}✗${RESET} %s\n" "$1"; }
info() { printf "${BLUE}→${RESET} %s\n" "$1"; }
warn() { printf "${YELLOW}!${RESET} %s\n" "$1"; }

# ── Parse arguments ──────────────────────────────────────────────────────────

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dir)
      INSTALL_DIR="$2"
      shift 2
      ;;
    --dir=*)
      INSTALL_DIR="${1#*=}"
      shift
      ;;
    --help|-h)
      echo "Usage: $0 [--dir /path/to/install]"
      echo ""
      echo "Downloads and starts mcp-awareness with a public tunnel URL."
      echo "Default install directory: $DEFAULT_DIR"
      exit 0
      ;;
    *)
      fail "Unknown option: $1"
      echo "Run '$0 --help' for usage."
      exit 1
      ;;
  esac
done

# ── Welcome ──────────────────────────────────────────────────────────────────

echo ""
printf "${BOLD}mcp-awareness demo installer${RESET}\n"
echo "─────────────────────────────"
echo ""
echo "This will:"
echo "  1. Download a Docker Compose file"
echo "  2. Start three containers (awareness server, Postgres, Cloudflare tunnel)"
echo "  3. Give you a public URL to connect from any MCP client"
echo ""

# ── Pre-flight checks ───────────────────────────────────────────────────────

info "Running pre-flight checks..."
echo ""
CHECKS_PASSED=true

# OS check
case "$(uname -s)" in
  Linux*|Darwin*)
    ok "Operating system: $(uname -s)"
    ;;
  CYGWIN*|MINGW*|MSYS*)
    fail "Windows is not supported. Please use WSL (Windows Subsystem for Linux)."
    echo "  Install WSL: https://learn.microsoft.com/en-us/windows/wsl/install"
    exit 1
    ;;
  *)
    warn "Unrecognized OS: $(uname -s). Proceeding anyway."
    ;;
esac

# curl
if command -v curl &>/dev/null; then
  ok "curl is installed"
else
  fail "curl is not installed."
  echo "  Install it with your package manager, e.g.:"
  echo "    Ubuntu/Debian: sudo apt install curl"
  echo "    macOS: brew install curl"
  exit 1
fi

# docker
if command -v docker &>/dev/null; then
  ok "Docker is installed"
else
  fail "Docker is not installed."
  echo "  Install it from: https://docs.docker.com/get-docker/"
  exit 1
fi

# docker daemon running
if docker info &>/dev/null; then
  ok "Docker daemon is running"
else
  fail "Docker daemon is not running."
  echo "  Start it with:"
  echo "    Linux:  sudo systemctl start docker"
  echo "    macOS:  Open Docker Desktop"
  exit 1
fi

# docker compose v2
if docker compose version &>/dev/null; then
  COMPOSE_VERSION=$(docker compose version --short 2>/dev/null || echo "unknown")
  ok "Docker Compose v2 is available ($COMPOSE_VERSION)"
else
  fail "Docker Compose v2 is not available."
  echo "  'docker compose' (with a space) is required — the old 'docker-compose' won't work."
  echo "  Update Docker Desktop, or install the compose plugin:"
  echo "    https://docs.docker.com/compose/install/"
  exit 1
fi

# internet connectivity
if curl -sf --max-time 5 "https://ghcr.io" -o /dev/null 2>/dev/null || \
   curl -sf --max-time 5 "https://github.com" -o /dev/null 2>/dev/null; then
  ok "Internet connectivity looks good"
else
  fail "Can't reach GitHub or GHCR. Check your internet connection."
  exit 1
fi

# conflicting containers
CONFLICTS=""
for name in mcp-awareness-demo mcp-awareness-demo-postgres mcp-awareness-demo-tunnel; do
  if docker ps -a --format '{{.Names}}' | grep -qx "$name"; then
    CONFLICTS="$CONFLICTS $name"
  fi
done
if [ -n "$CONFLICTS" ]; then
  fail "These container names are already in use:$CONFLICTS"
  echo ""
  echo "  If these are from a previous install, remove them with:"
  echo "    docker compose -p mcp-awareness-demo down"
  echo ""
  echo "  Or remove them individually:"
  for name in $CONFLICTS; do
    echo "    docker rm -f $name"
  done
  exit 1
fi

echo ""
ok "All checks passed!"
echo ""

# ── Choose install directory ─────────────────────────────────────────────────

if [ -z "$INSTALL_DIR" ]; then
  printf "Where should we put the compose file? [${BOLD}%s${RESET}]: " "$DEFAULT_DIR"
  read -r INSTALL_DIR
  INSTALL_DIR="${INSTALL_DIR:-$DEFAULT_DIR}"
fi

# Expand ~ if present
INSTALL_DIR="${INSTALL_DIR/#\~/$HOME}"

if [ -f "$INSTALL_DIR/docker-compose-demo.yaml" ]; then
  warn "A docker-compose-demo.yaml already exists in $INSTALL_DIR"
  printf "Overwrite it? [y/N]: "
  read -r OVERWRITE
  if [[ ! "$OVERWRITE" =~ ^[Yy] ]]; then
    echo "Cancelled."
    exit 0
  fi
fi

mkdir -p "$INSTALL_DIR"
ok "Install directory: $INSTALL_DIR"

# ── Download compose file ────────────────────────────────────────────────────

if [ -f "$INSTALL_DIR/docker-compose-demo.yaml" ]; then
  ok "Using existing docker-compose-demo.yaml"
else
  info "Downloading docker-compose-demo.yaml..."
  if curl -sfL "$COMPOSE_URL" -o "$INSTALL_DIR/docker-compose-demo.yaml"; then
    ok "Downloaded compose file"
  else
    fail "Failed to download compose file from:"
    echo "  $COMPOSE_URL"
    exit 1
  fi
fi

# ── Start services ───────────────────────────────────────────────────────────

echo ""
info "Starting services..."
echo "  This pulls three Docker images on first run — it may take a minute or two."
echo ""

if ! docker compose -f "$INSTALL_DIR/docker-compose-demo.yaml" up -d 2>&1; then
  fail "Failed to start services."
  echo "  Check the logs with: docker compose -f $INSTALL_DIR/docker-compose-demo.yaml logs"
  exit 1
fi

echo ""
ok "Services are starting"

# ── Wait for tunnel URL ─────────────────────────────────────────────────────

echo ""
info "Waiting for the tunnel URL (this usually takes 10–15 seconds)..."

TUNNEL_URL=""
for i in $(seq 1 30); do
  TUNNEL_URL=$(docker compose -f "$INSTALL_DIR/docker-compose-demo.yaml" logs tunnel 2>&1 \
    | grep -oE 'https://[a-zA-Z0-9_-]+\.trycloudflare\.com' \
    | head -1 || true)
  if [ -n "$TUNNEL_URL" ]; then
    break
  fi
  sleep 1
done

if [ -z "$TUNNEL_URL" ]; then
  warn "Couldn't detect the tunnel URL yet."
  echo "  The services may still be starting. Check manually with:"
  echo "    docker compose -f $INSTALL_DIR/docker-compose-demo.yaml logs tunnel"
  echo ""
  echo "  Look for a line containing 'trycloudflare.com'"
  exit 0
fi

MCP_URL="$TUNNEL_URL/mcp"

# ── Success ──────────────────────────────────────────────────────────────────

echo ""
echo "─────────────────────────────────────────────────────────────────"
printf "${GREEN}${BOLD}mcp-awareness is running!${RESET}\n"
echo "─────────────────────────────────────────────────────────────────"
echo ""
printf "${BOLD}Your MCP URL:${RESET} %s\n" "$MCP_URL"
echo ""
warn "This URL is temporary — it changes every time you restart."
echo "  For a stable URL, see the Deployment Guide:"
echo "  https://github.com/cmeans/mcp-awareness/blob/main/docs/deployment-guide.md"
echo ""

# ── Config snippets ──────────────────────────────────────────────────────────

printf "${BOLD}Connect your AI:${RESET}\n"
echo ""

printf "${BOLD}Claude.ai${RESET} (Settings → Connectors → Add custom connector):\n"
echo "  Name: awareness"
echo "  URL:  $MCP_URL"
echo ""

printf "${BOLD}Claude Desktop / Claude Code${RESET} (add to MCP settings):\n"
cat <<EOF
  {
    "mcpServers": {
      "awareness": {
        "url": "$MCP_URL"
      }
    }
  }
EOF
echo ""

printf "${BOLD}Cursor${RESET} (add to .cursor/mcp.json):\n"
cat <<EOF
  {
    "mcpServers": {
      "awareness": {
        "url": "$MCP_URL"
      }
    }
  }
EOF
echo ""

printf "${BOLD}VS Code${RESET} (add to .vscode/mcp.json):\n"
cat <<EOF
  {
    "servers": {
      "awareness": {
        "type": "http",
        "url": "$MCP_URL"
      }
    }
  }
EOF
echo ""

# ── Getting started ──────────────────────────────────────────────────────────

echo "─────────────────────────────────────────────────────────────────"
printf "${BOLD}Getting started:${RESET}\n"
echo ""
echo "  Your instance is pre-loaded with demo data. Start a conversation"
echo "  and your AI will discover it automatically via the briefing."
echo ""
echo "  If your client supports prompts, try the \"getting-started\" prompt"
echo "  to personalize your instance — your AI will interview you and"
echo "  store what it learns."
echo ""
warn "Best experience with Claude Sonnet 4.6 or Opus 4.6."
echo "  Smaller models (Haiku, GPT-4o-mini) may not follow MCP prompts reliably."
echo ""

# ── Management ───────────────────────────────────────────────────────────────

echo "─────────────────────────────────────────────────────────────────"
printf "${BOLD}Managing your installation:${RESET}\n"
echo ""
echo "  Compose file: $INSTALL_DIR/docker-compose-demo.yaml"
echo ""
echo "  View logs:    docker compose -f $INSTALL_DIR/docker-compose-demo.yaml logs -f"
echo "  Stop:         docker compose -f $INSTALL_DIR/docker-compose-demo.yaml down"
echo "  Restart:      docker compose -f $INSTALL_DIR/docker-compose-demo.yaml up -d"
echo "                (you'll get a new tunnel URL — check logs for it)"
echo ""
echo "  Remove everything (including your data):"
echo "    docker compose -f $INSTALL_DIR/docker-compose-demo.yaml down -v"
echo ""
