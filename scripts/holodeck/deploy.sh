#!/usr/bin/env bash
# mcp-awareness — ambient system awareness for AI agents
# Copyright (C) 2026 Chris Means
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

# Zero-downtime deploy for mcp-awareness on holodeck.
# Run from any host with SSH access to app nodes and HAProxy.
# Usage: deploy.sh hot              — rolling code update, zero-downtime
#        deploy.sh maintenance      — full stop, migrate, restart (scheduled)
set -euo pipefail

HAPROXY_HOST="192.168.200.103"
HAPROXY_SOCK="/var/run/haproxy/admin.sock"
APP_NODES=("192.168.200.110:app-a" "192.168.200.111:app-b")
DRAIN_TIMEOUT=60
HEALTH_TIMEOUT=30
HEALTH_INTERVAL=2

MODE="${1:?Usage: deploy.sh <hot|maintenance>}"

# --- Helpers ---

haproxy_cmd() {
    ssh "root@${HAPROXY_HOST}" "echo '$1' | socat stdio ${HAPROXY_SOCK}"
}

node_ip() { echo "${1%%:*}"; }
node_name() { echo "${1##*:}"; }

drain_node() {
    local name="$1"
    echo "  Draining ${name}..."
    haproxy_cmd "set server awareness-backend/${name} state drain"

    local waited=0
    while (( waited < DRAIN_TIMEOUT )); do
        local conns
        conns=$(haproxy_cmd "show stat" | grep "awareness-backend,${name}," | cut -d, -f5)
        if [[ "${conns:-0}" == "0" ]]; then
            echo "  ${name}: all connections drained"
            return 0
        fi
        echo "  ${name}: ${conns} active connections, waiting..."
        sleep 5
        waited=$((waited + 5))
    done
    echo "  WARNING: ${name} drain timeout (${DRAIN_TIMEOUT}s), proceeding anyway"
}

enable_node() {
    local name="$1"
    haproxy_cmd "set server awareness-backend/${name} state ready"
    echo "  ${name}: re-enabled"
}

update_node() {
    local ip="$1"
    echo "  Updating ${ip}..."
    ssh "root@${ip}" 'cd /opt/mcp-awareness && sudo -u awareness git pull origin main && sudo -u awareness venv/bin/pip install -e . -q && systemctl restart mcp-awareness'
}

wait_healthy() {
    local ip="$1"
    local waited=0
    while (( waited < HEALTH_TIMEOUT )); do
        if curl -sf "http://${ip}:8420/health" > /dev/null 2>&1; then
            echo "  ${ip}: healthy"
            return 0
        fi
        sleep "$HEALTH_INTERVAL"
        waited=$((waited + HEALTH_INTERVAL))
    done
    echo "  ERROR: ${ip} failed health check after ${HEALTH_TIMEOUT}s"
    return 1
}

# --- Hot deploy (rolling, zero-downtime) ---

hot_deploy() {
    echo "=== Hot deploy (zero-downtime) ==="
    for entry in "${APP_NODES[@]}"; do
        local ip name
        ip=$(node_ip "$entry")
        name=$(node_name "$entry")

        echo ""
        echo "--- ${name} (${ip}) ---"
        drain_node "$name"
        update_node "$ip"

        if wait_healthy "$ip"; then
            enable_node "$name"
        else
            echo "  ABORT: ${name} failed health check — leaving drained!"
            echo "  Manual intervention required. Remaining nodes untouched."
            exit 1
        fi
    done

    echo ""
    echo "=== Hot deploy complete ==="
}

# --- Maintenance deploy (full stop, migrate, restart) ---

maintenance_deploy() {
    echo "=== Maintenance deploy ==="
    echo ""

    # Drain all nodes
    echo "Step 1: Draining all nodes..."
    for entry in "${APP_NODES[@]}"; do
        drain_node "$(node_name "$entry")"
    done

    echo ""
    echo "Step 2: Updating first node and running migration..."
    local first_ip
    first_ip=$(node_ip "${APP_NODES[0]}")
    update_node "$first_ip"
    ssh "root@${first_ip}" 'cd /opt/mcp-awareness && sudo -u awareness bash -c "set -a && source /etc/awareness/env && set +a && /opt/mcp-awareness/venv/bin/mcp-awareness-migrate upgrade head"'
    echo "  Migration complete on ${first_ip}"
    wait_healthy "$first_ip" || echo "  WARNING: ${first_ip} not healthy after migration"

    echo ""
    echo "Step 3: Updating remaining nodes..."
    for entry in "${APP_NODES[@]:1}"; do
        local ip
        ip=$(node_ip "$entry")
        update_node "$ip"
        wait_healthy "$ip" || echo "  WARNING: ${ip} not healthy yet"
    done

    echo ""
    echo "Step 4: Re-enabling all nodes..."
    for entry in "${APP_NODES[@]}"; do
        enable_node "$(node_name "$entry")"
    done

    echo ""
    echo "=== Maintenance deploy complete ==="
}

# --- Main ---

case "$MODE" in
    hot)
        hot_deploy
        ;;
    maintenance)
        echo "This will briefly take the service offline for migrations."
        read -p "Continue? [y/N] " -r
        [[ $REPLY =~ ^[Yy]$ ]] || exit 0
        maintenance_deploy
        ;;
    *)
        echo "Usage: deploy.sh <hot|maintenance>" >&2
        exit 1
        ;;
esac
