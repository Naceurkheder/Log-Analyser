#!/usr/bin/env bash
# Sentinel-GPU one-shot installer / runner / closer.
#
# Everything is local to this repository. Runtime state (PIDs, logs, generated
# .env) stays under .local/sentinel/ and is ignored by git.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$ROOT"

STATE_DIR="$ROOT/.local/sentinel"
mkdir -p "$STATE_DIR"

VENV="${VENV:-.venv}"
API_HOST="${API_HOST:-127.0.0.1}"
API_PORT="${API_PORT:-8000}"
DASH_HOST="${DASH_HOST:-127.0.0.1}"
DASH_PORT="${DASH_PORT:-8501}"
BACKEND="${BACKEND:-auto}"
DATASET_MB="${DATASET_MB:-100}"
SPARK_MASTER="${SPARK_MASTER:-local[*]}"
SPARK_COMMITTER="${SPARK_COMMITTER:-worker-fenced}"
AWS_ENDPOINT_URL="${AWS_ENDPOINT_URL:-http://10.20.0.10:4566}"

USE_SPARK=0
USE_MONITORING=0
WITH_GPU=0
KEEP_FLEET=0
SKIP_DATASET=0
RUN_ID="${RUN_ID:-}"

log()  { printf '\033[36m[sentinel]\033[0m %s\n' "$*"; }
warn() { printf '\033[33m[sentinel]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[31m[sentinel]\033[0m %s\n' "$*" >&2; exit 1; }

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || die "missing required command: $1 (install it, then re-run)"
}

usage() {
  cat <<'USAGE'
Sentinel-GPU lifecycle script

Usage:
  scripts/sentinel.sh <command> [options]

Commands:
  install   Create venv, install dependencies, create .env and generate dataset
  run       Start LXD fleet, emulated AWS, API, producer, workers and dashboard
  status    Show process, endpoint and container state
  close     Stop started processes, monitoring and (by default) the LXD fleet
  restart   close --keep-fleet, then run
  spark     Run an optional Spark batch stage for a RUN_ID
  help      Show this text

Options:
  --venv PATH            Virtualenv path (default: .venv)
  --api-host HOST        API bind host (default: 127.0.0.1)
  --api-port PORT        API port (default: 8000)
  --dashboard-host HOST  Dashboard bind host (default: 127.0.0.1)
  --dashboard-port PORT  Dashboard port (default: 8501)
  --backend NAME         auto|cpu|gpu (default: auto)
  --dataset-mb N         Synthetic dataset size for install (default: 100)
  --spark                Install pyspark from requirements-spark.txt
  --monitoring           Start Prometheus/Grafana via monitoring/docker-compose.yml
  --with-gpu             Start the GPU worker if nvidia-smi is available
  --keep-fleet           For close/restart, leave the LXD fleet running
  --skip-dataset         Do not regenerate data/bench.log during install
  --run-id ID            Run ID for the spark command
  --committer NAME       Spark result committer: worker-fenced|logging
                         (default: worker-fenced)
  --help                 Show this text

Environment equivalents: VENV, API_HOST, API_PORT, DASH_HOST, DASH_PORT,
BACKEND, DATASET_MB, SPARK_MASTER, SPARK_COMMITTER, AWS_ENDPOINT_URL, RUN_ID.

Examples:
  scripts/sentinel.sh install --spark
  scripts/sentinel.sh run --with-gpu --monitoring --backend gpu
  scripts/sentinel.sh spark --run-id run_abc123
  scripts/sentinel.sh status
  scripts/sentinel.sh close --keep-fleet
USAGE
}

parse_flags() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --venv)           VENV="$2"; shift 2 ;;
      --api-host)       API_HOST="$2"; shift 2 ;;
      --api-port)       API_PORT="$2"; shift 2 ;;
      --dashboard-host) DASH_HOST="$2"; shift 2 ;;
      --dashboard-port) DASH_PORT="$2"; shift 2 ;;
      --backend)        BACKEND="$2"; shift 2 ;;
      --dataset-mb)     DATASET_MB="$2"; shift 2 ;;
      --run-id)         RUN_ID="$2"; shift 2 ;;
      --committer)      SPARK_COMMITTER="$2"; shift 2 ;;
      --spark)          USE_SPARK=1; shift ;;
      --monitoring)     USE_MONITORING=1; shift ;;
      --with-gpu)       WITH_GPU=1; shift ;;
      --keep-fleet)     KEEP_FLEET=1; shift ;;
      --skip-dataset)   SKIP_DATASET=1; shift ;;
      --help|-h)        usage; exit 0 ;;
      *) die "unknown option: $1 (see --help)" ;;
    esac
  done

  case "$BACKEND" in
    auto|cpu|gpu) ;;
    *) die "--backend must be auto, cpu or gpu" ;;
  esac

  case "$SPARK_COMMITTER" in
    worker-fenced|logging) ;;
    *) die "--committer must be worker-fenced or logging" ;;
  esac

  export PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}"
  export AWS_ENDPOINT_URL AWS_REGION="${AWS_REGION:-us-east-1}"
  export AWS_ACCESS_KEY_ID="${AWS_ACCESS_KEY_ID:-test}"
  export AWS_SECRET_ACCESS_KEY="${AWS_SECRET_ACCESS_KEY:-test}"
  export SENTINEL_API_HOST="$API_HOST"
  export SENTINEL_API_PORT="$API_PORT"
  export SENTINEL_API_URL="http://$API_HOST:$API_PORT"
}

py() {
  if [[ -x "$ROOT/$VENV/bin/python" ]]; then
    printf '%s\n' "$ROOT/$VENV/bin/python"
  else
    printf '%s\n' "$ROOT/$VENV/bin/python"
  fi
}

ensure_env_file() {
  if [[ ! -f "$ROOT/.env" ]]; then
    cp "$ROOT/.env.example" "$ROOT/.env"
    log "created .env from .env.example (dummy local credentials only)"
  fi
}

cmd_install() {
  need_cmd python3
  need_cmd terraform
  ensure_env_file

  log "creating virtualenv: $VENV"
  python3 -m venv "$ROOT/$VENV"
  local python_bin
  python_bin="$(py)"
  "$python_bin" -m pip install --quiet --upgrade pip
  "$python_bin" -m pip install --quiet -r requirements.txt -r requirements-dev.txt

  if [[ $USE_SPARK -eq 1 ]]; then
    log "installing optional Spark dependencies"
    "$python_bin" -m pip install --quiet -r requirements-spark.txt
  fi

  if [[ $SKIP_DATASET -eq 0 ]]; then
    log "generating ${DATASET_MB} MB synthetic dataset"
    mkdir -p "$ROOT/data"
    "$python_bin" scripts/gen_dataset.py --size-mb "$DATASET_MB" \
      --out data/bench.log --labels-out data/bench.labels.json
  fi

  log "initialising Terraform working directory"
  terraform -chdir="$ROOT/terraform" init -reconfigure -input=false
  log "install complete — run: scripts/sentinel.sh run"
}

start_bg() {
  local name="$1"
  shift
  local pidfile="$STATE_DIR/$name.pid"
  local logfile="$STATE_DIR/$name.log"

  if [[ -f "$pidfile" ]] && kill -0 "$(cat "$pidfile")" 2>/dev/null; then
    log "$name already running (pid $(cat "$pidfile"))"
    return 0
  fi

  nohup "$@" >"$logfile" 2>&1 &
  echo $! >"$pidfile"
  log "started $name (pid $!, log $logfile)"
}

wait_api() {
  local target="127.0.0.1"
  [[ "$API_HOST" == "0.0.0.0" ]] || target="$API_HOST"
  for _ in {1..40}; do
    if curl -fsS "http://$target:$API_PORT/healthz" >/dev/null 2>&1; then
      log "API healthy at http://$target:$API_PORT"
      return 0
    fi
    sleep 1
  done
  warn "API did not become healthy; inspect $STATE_DIR/api.log"
  return 1
}

cmd_run() {
  local python_bin
  python_bin="$(py)"
  [[ -x "$python_bin" ]] || die "virtualenv missing — run: scripts/sentinel.sh install"
  need_cmd curl
  need_cmd lxc
  need_cmd terraform
  ensure_env_file

  log "starting LXD fleet and emulated AWS"
  ./lxd/fleet.sh up
  make infra

  start_bg api "$python_bin" -m sentinel.api.cli serve \
    --host "$API_HOST" --port "$API_PORT"
  wait_api || true

  start_bg producer "$python_bin" -m sentinel.api.cli produce --watch --wait 20
  start_bg cpu-worker env SENTINEL_WORKER_METRICS_PORT=8083 \
    "$python_bin" -m sentinel.worker.cli serve --backend cpu --loop

  if [[ $WITH_GPU -eq 1 ]]; then
    if command -v nvidia-smi >/dev/null 2>&1; then
      start_bg gpu-worker env SENTINEL_WORKER_METRICS_PORT=8082 \
        "$python_bin" -m sentinel.worker.cli serve --backend gpu --loop
    else
      warn "nvidia-smi not found; GPU worker was not started"
    fi
  fi

  start_bg dashboard "$python_bin" -m streamlit run dashboard_app.py \
    --server.address "$DASH_HOST" --server.port "$DASH_PORT" --server.headless true

  if [[ $USE_MONITORING -eq 1 ]]; then
    need_cmd docker
    log "starting Prometheus and Grafana"
    docker compose -f monitoring/docker-compose.yml up -d
  fi

  log "backend policy: $BACKEND"
  log "API:       http://$API_HOST:$API_PORT/healthz"
  log "Dashboard: http://$DASH_HOST:$DASH_PORT"
  log "Logs:      $STATE_DIR/*.log"
}

cmd_status() {
  log "managed processes"
  local found=0
  for pidfile in "$STATE_DIR"/*.pid; do
    [[ -e "$pidfile" ]] || continue
    found=1
    local name pid
    name="$(basename "$pidfile" .pid)"
    pid="$(cat "$pidfile")"
    if kill -0 "$pid" 2>/dev/null; then
      printf '  %-12s running  pid=%s\n' "$name" "$pid"
    else
      printf '  %-12s stopped  pid=%s\n' "$name" "$pid"
    fi
  done
  [[ $found -eq 1 ]] || printf '  none\n'

  if command -v curl >/dev/null 2>&1; then
    local target="127.0.0.1"
    [[ "$API_HOST" == "0.0.0.0" ]] || target="$API_HOST"
    log "endpoints"
    curl -fsS "http://$target:$API_PORT/healthz" 2>/dev/null || warn "API not reachable"
    printf '\n'
  fi

  if command -v lxc >/dev/null 2>&1; then
    log "LXD fleet"
    ./lxd/fleet.sh status || true
  fi
}

stop_pids() {
  local stopped=0
  for pidfile in "$STATE_DIR"/*.pid; do
    [[ -e "$pidfile" ]] || continue
    local name pid
    name="$(basename "$pidfile" .pid)"
    pid="$(cat "$pidfile")"
    if kill -0 "$pid" 2>/dev/null; then
      pkill -TERM -P "$pid" 2>/dev/null || true
      kill -TERM "$pid" 2>/dev/null || true
      for _ in {1..15}; do
        kill -0 "$pid" 2>/dev/null || break
        sleep 0.2
      done
      if kill -0 "$pid" 2>/dev/null; then
        kill -KILL "$pid" 2>/dev/null || true
      fi
      log "stopped $name"
      stopped=1
    fi
    rm -f "$pidfile"
  done
  [[ $stopped -eq 1 ]] || log "no managed processes were running"
}

cmd_close() {
  stop_pids

  if [[ -f monitoring/docker-compose.yml ]] && command -v docker >/dev/null 2>&1; then
    log "stopping monitoring stack"
    docker compose -f monitoring/docker-compose.yml down 2>/dev/null || true
  fi

  if [[ $KEEP_FLEET -eq 0 ]] && command -v lxc >/dev/null 2>&1; then
    log "stopping LXD fleet"
    ./lxd/fleet.sh down || true
  elif [[ $KEEP_FLEET -eq 1 ]]; then
    log "leaving LXD fleet running (--keep-fleet)"
  fi

  log "closed — run: scripts/sentinel.sh status"
}

cmd_restart() {
  KEEP_FLEET=1 cmd_close
  cmd_run
}

cmd_spark() {
  [[ -n "$RUN_ID" ]] || die "spark requires --run-id ID"
  local python_bin
  python_bin="$(py)"
  [[ -x "$python_bin" ]] || die "virtualenv missing — run: scripts/sentinel.sh install --spark"
  "$python_bin" -c 'import pyspark' 2>/dev/null || die "pyspark missing — run: scripts/sentinel.sh install --spark"

  log "starting Spark batch for $RUN_ID (master $SPARK_MASTER)"
  "$python_bin" -m sentinel.spark.cli run --run-id "$RUN_ID" \
    --master "$SPARK_MASTER" --endpoint-url "$AWS_ENDPOINT_URL" \
    --committer "$SPARK_COMMITTER"
}

main() {
  [[ $# -gt 0 ]] || { usage; exit 1; }
  local command="$1"
  shift
  parse_flags "$@"

  case "$command" in
    install)  cmd_install ;;
    run)      cmd_run ;;
    status)   cmd_status ;;
    close)    cmd_close ;;
    restart)  cmd_restart ;;
    spark)    cmd_spark ;;
    help|--help|-h) usage ;;
    *) die "unknown command: $command (see --help)" ;;
  esac
}

main "$@"
