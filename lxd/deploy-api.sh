#!/usr/bin/env bash
#
# lxd/deploy-api.sh — Push the control-plane package into `api` and restart.
#
# Fast path for development: only the Python source is copied; dependencies
# and the systemd unit come from lxd/bootstrap-api.sh.
#
# Usage: ./lxd/deploy-api.sh [container]

set -euo pipefail

CONTAINER="${1:-api}"
PORT="${2:-8080}"
ROOT=/opt/sentinel
SRC="$ROOT/src"
HOST_IP="${SENTINEL_API_IP:-10.20.0.11}"

REPO="$(cd "$(dirname "$0")/.." && pwd)"

# shellcheck source=lxd/common.sh
. "$(dirname "$0")/common.sh"

lxd_require >/dev/null || exit 1
lxd_instance_exists "$CONTAINER" || {
    echo "Error: container '$CONTAINER' not found in project '$PROJECT'" >&2
    exit 1
}

echo ">>> pushing src/sentinel to $CONTAINER:$SRC"
lxd_exec "$CONTAINER" mkdir -p "$SRC"
"${LXC[@]}" file push -r "$REPO/src/sentinel" "$CONTAINER$SRC/"

echo ">>> restarting sentinel-api.service"
lxd_exec "$CONTAINER" systemctl restart sentinel-api.service

echo -n ">>> waiting for http://$HOST_IP:$PORT/healthz"
for _ in $(seq 1 30); do
    if curl -sf "http://$HOST_IP:$PORT/healthz" >/dev/null 2>&1; then
        echo " OK"
        curl -s "http://$HOST_IP:$PORT/readyz" || true
        echo
        exit 0
    fi
    echo -n "."
    sleep 1
done
echo " TIMEOUT" >&2
echo "Hint: lxc --project sentinel exec $CONTAINER -- journalctl -u sentinel-api -n 50" >&2
exit 1
