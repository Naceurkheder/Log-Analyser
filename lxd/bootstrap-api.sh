#!/usr/bin/env bash
#
# lxd/bootstrap-api.sh — Provision the Sentinel-GPU control API inside `api`.
#
# Creates an isolated venv, installs the runtime dependencies, writes the
# environment file consumed by systemd, and enables the service on
# 0.0.0.0:8080 (the address the fleet documentation promises).
#
# Idempotent: safe to run repeatedly. Code is copied by lxd/deploy-api.sh so
# the image is never rebuilt just to ship a Python change.

set -euo pipefail

CONTAINER="${1:-api}"
PORT="${2:-8080}"
ROOT=/opt/sentinel
VENV="$ROOT/venv"
SRC="$ROOT/src"

# shellcheck source=lxd/common.sh
. "$(dirname "$0")/common.sh"

lxd_require >/dev/null || exit 1
lxd_instance_exists "$CONTAINER" || {
    echo "Error: container '$CONTAINER' not found in project '$PROJECT'" >&2
    exit 1
}

run() { lxd_exec "$CONTAINER" "$@"; }

echo ">>> [$CONTAINER] installing system packages"
run apt-get update -qq
run bash -c "DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3 python3-venv ca-certificates >/dev/null"

echo ">>> [$CONTAINER] creating venv and installing API dependencies"
run bash -c "$VENV/bin/python -V >/dev/null 2>&1 || python3 -m venv $VENV"
run bash -c "$VENV/bin/pip install --quiet --upgrade pip"
run bash -c "$VENV/bin/pip install --quiet 'fastapi>=0.115,<1' 'uvicorn>=0.30,<1' 'boto3>=1.35,<2' 'pydantic>=2.7,<3'"

echo ">>> [$CONTAINER] preparing directories and environment"
run mkdir -p "$SRC" /etc/sentinel
run bash -c "cat > /etc/sentinel/api.env <<EOF
AWS_ENDPOINT_URL=http://10.20.0.10:4566
AWS_REGION=us-east-1
AWS_DEFAULT_REGION=us-east-1
AWS_ACCESS_KEY_ID=test
AWS_SECRET_ACCESS_KEY=test
SENTINEL_INCOMING_BUCKET=incoming
SENTINEL_RUNS_TABLE=SentinelRuns
SENTINEL_JOBS_TABLE=SentinelJobs
SENTINEL_ALERTS_TABLE=SentinelAlerts
SENTINEL_INGEST_QUEUE=ingest-queue
SENTINEL_LOG_LEVEL=INFO
SENTINEL_API_URL=http://10.20.0.11:$PORT
EOF"

echo ">>> [$CONTAINER] writing systemd unit (sentinel-api.service)"
run bash -c "cat > /etc/systemd/system/sentinel-api.service <<EOF
[Unit]
Description=Sentinel-GPU control API
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
EnvironmentFile=/etc/sentinel/api.env
Environment=PYTHONPATH=$SRC
WorkingDirectory=$ROOT
ExecStart=$VENV/bin/python -m sentinel.api.cli serve --host 0.0.0.0 --port $PORT
Restart=always
RestartSec=2

[Install]
WantedBy=multi-user.target
EOF"

run systemctl daemon-reload
run systemctl enable sentinel-api.service

echo ">>> [$CONTAINER] bootstrap complete; run lxd/deploy-api.sh to ship the code"
