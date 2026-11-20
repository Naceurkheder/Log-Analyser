#!/usr/bin/env bash
#
# lxd/bootstrap-infra.sh — Provision the emulated-AWS service inside `infra`.
#
# Installs moto (lightweight AWS emulation: S3 / SQS / DynamoDB) in an isolated
# venv and runs it as a systemd service on 0.0.0.0:4566, reachable from the
# host and every container on the fleet bridge.
#
# Why moto instead of LocalStack: the project is academic and runs on a
# laptop — moto needs no Docker daemon, installs in ~100 MB, and covers the
# S3/SQS/DynamoDB surface this project uses. See docs/decisions/0002-*.md.
#
# moto_server runs a ThreadedMotoServer (werkzeug), so concurrent SQS long polls
# and parallel worker requests are handled without an extra WSGI layer.
#
# Idempotent: safe to run repeatedly.

set -euo pipefail

CONTAINER="${1:-infra}"
PORT="${2:-4566}"
VENV=/opt/moto/venv

# shellcheck source=lxd/common.sh
. "$(dirname "$0")/common.sh"

lxd_require >/dev/null || exit 1
lxd_instance_exists "$CONTAINER" || { echo "Error: container '$CONTAINER' not found in project '$PROJECT'" >&2; exit 1; }

run() { lxd_exec "$CONTAINER" "$@"; }

echo ">>> [$CONTAINER] installing system packages"
run apt-get update -qq
run bash -c "DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3 python3-venv python3-pip ca-certificates >/dev/null"

echo ">>> [$CONTAINER] creating venv and installing moto + gunicorn"
run bash -c "python3 -m venv $VENV && $VENV/bin/pip install --quiet --upgrade pip wheel"
run bash -c "$VENV/bin/pip install --quiet 'moto[server]' gunicorn"

echo ">>> [$CONTAINER] writing systemd unit (sentinel-moto.service)"
run bash -c "cat > /etc/systemd/system/sentinel-moto.service <<EOF
[Unit]
Description=Sentinel-GPU emulated AWS (moto)
After=network.target

[Service]
ExecStart=$VENV/bin/moto_server -H 0.0.0.0 -p $PORT
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF"

echo ">>> [$CONTAINER] enabling service"
run systemctl daemon-reload
run systemctl enable --now sentinel-moto.service

# Wait for the API to answer
echo -n ">>> waiting for http://$CONTAINER:$PORT"
for _ in $(seq 1 30); do
    if lxd_exec "$CONTAINER" bash -c "curl -sf http://127.0.0.1:$PORT/moto-api/ >/dev/null" 2>/dev/null; then
        echo " OK"
        exit 0
    fi
    echo -n "."
    sleep 2
done
echo " TIMEOUT" >&2
exit 1
