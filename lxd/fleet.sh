#!/usr/bin/env bash
#
# lxd/fleet.sh — Idempotent lifecycle manager for the Sentinel-GPU LXD fleet.
#
# The academic deployment runs a reduced topology (sized to a 16 GB laptop):
#
#   infra         1536 MiB   moto (emulated AWS: S3 / SQS / DynamoDB)
#   gpu-worker-1  1536 MiB   CUDA worker
#   cpu-worker-1  1024 MiB   CPU reference worker
#   api            512 MiB   FastAPI run/status API (Sprint 3)
#   producer       512 MiB   streaming chunker / ingest consumer (Sprint 3)
#   dashboard      512 MiB   Streamlit presentation layer
#
# Terraform runs on the HOST (no dedicated provision container) and targets
# the emulated AWS API through the private bridge.
#
# Usage: ./lxd/fleet.sh <up|down|status|destroy|shell|dns-check> [container]

set -euo pipefail

# --------------------------------------------------------------------------
# Configuration (overridable via environment / .env)
# --------------------------------------------------------------------------
# shellcheck source=lxd/common.sh
. "$(dirname "$0")/common.sh"

NET="${SENTINEL_NET:-sentinel-net}"
NET_CIDR="${SENTINEL_NET_CIDR:-10.20.0.1/24}"
IMAGE="${FLEET_IMAGE:-ubuntu:22.04}"

declare -A CONTAINERS=(
    [infra]=1536MiB
    [gpu-worker-1]=1536MiB
    [cpu-worker-1]=1024MiB
    [api]=512MiB
    [producer]=512MiB
    [dashboard]=512MiB
)
declare -A CPU_ALLOWANCE=(
    [infra]=50%
    [gpu-worker-1]=300%
    [cpu-worker-1]=200%
    [api]=100%
    [producer]=100%
    [dashboard]=50%
)
declare -A IPV4=(
    [infra]=10.20.0.10
    [gpu-worker-1]=10.20.0.21
    [cpu-worker-1]=10.20.0.22
    [api]=10.20.0.11
    [producer]=10.20.0.12
    [dashboard]=10.20.0.23
)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
die() { echo "Error: $*" >&2; exit 1; }

preflight() {
    command -v lxc >/dev/null 2>&1 || die "'lxc' not found. Install LXD first."
    lxc info >/dev/null 2>&1 || die "cannot reach the LXD daemon (running? in 'lxd' group?)"
}

ensure_project() {
    "${LXC[@]}" project show "$PROJECT" >/dev/null 2>&1 && return 0
    echo ">>> creating LXD project: $PROJECT"
    "${LXC[@]}" project create "$PROJECT" -c features.images=true -c features.profiles=false
}

ensure_network() {
    "${LXC[@]}" network show "$NET" >/dev/null 2>&1 && return 0
    echo ">>> creating bridge: $NET ($NET_CIDR)"
    "${LXC[@]}" network create "$NET" \
        ipv4.address="$NET_CIDR" \
        ipv4.nat=true \
        ipv4.dhcp=true \
        ipv6.address=none
}

ensure_container() {
    local name="$1" mem="$2" cpu="$3" ip="$4"

    if ! "${LXC[@]}" info "$name" >/dev/null 2>&1; then
        echo ">>> launching $name (mem cap $mem, cpu $cpu, ip $ip)"
        "${LXC[@]}" launch "$IMAGE" "$name" --profile default -q
        # Static NIC with a fixed address on the fleet bridge.
        "${LXC[@]}" config device add "$name" eth0 nic network="$NET" ipv4.address="$ip"
        "${LXC[@]}" config set "$name" limits.memory="$mem" limits.memory.swap=true \
            limits.cpu.allowance="$cpu"
    fi

    # Enforce caps even on pre-existing containers (caps are not just set — they are re-asserted).
    "${LXC[@]}" config set "$name" limits.memory="$mem" limits.cpu.allowance="$cpu"

    local state
    state="$("${LXC[@]}" list "$name" --format csv --columns s)"
    if [[ "$state" != "RUNNING" ]]; then
        echo ">>> starting $name (was $state)"
        "${LXC[@]}" start "$name"
    fi
}

wait_ready() {
    local name="$1"
    for _ in $(seq 1 60); do
        if "${LXC[@]}" exec "$name" -- cloud-init status --wait >/dev/null 2>&1; then return 0; fi
        sleep 2
    done
    echo "warning: $name did not report cloud-init done in 120s" >&2
}

# --------------------------------------------------------------------------
# Targets
# --------------------------------------------------------------------------
cmd_up() {
    preflight
    ensure_project
    ensure_network
    for name in "${!CONTAINERS[@]}"; do
        ensure_container "$name" "${CONTAINERS[$name]}" "${CPU_ALLOWANCE[$name]}" "${IPV4[$name]}"
    done
    for name in "${!CONTAINERS[@]}"; do wait_ready "$name"; done
    echo
    "${LXC[@]}" list
    echo
    echo "Fleet is up. Run 'make infra' to provision the emulated AWS resources."
}

cmd_down() {
    preflight
    for name in "${!CONTAINERS[@]}"; do
        local state
        state="$("${LXC[@]}" list "$name" --format csv --columns s 2>/dev/null || echo MISSING)"
        [[ "$state" == "RUNNING" ]] && { echo ">>> stopping $name"; "${LXC[@]}" stop "$name"; }
    done
}

cmd_status() {
    preflight
    "${LXC[@]}" list
    echo
    echo "Resource caps:"
    for name in "${!CONTAINERS[@]}"; do
        if "${LXC[@]}" info "$name" >/dev/null 2>&1; then
            printf '  %-13s mem=%s cpu=%s\n' "$name" \
                "$("${LXC[@]}" config get "$name" limits.memory)" \
                "$("${LXC[@]}" config get "$name" limits.cpu.allowance)"
        fi
    done
}

cmd_destroy() {
    preflight
    echo "!!! This deletes the fleet containers, network and project '$PROJECT'."
    read -r -p "Type 'destroy' to confirm: " answer
    [[ "$answer" == "destroy" ]] || die "aborted"
    for name in "${!CONTAINERS[@]}"; do
        "${LXC[@]}" delete "$name" --force 2>/dev/null && echo ">>> deleted $name" || true
    done
    "${LXC[@]}" network delete "$NET" 2>/dev/null || true
    "${LXC[@]}" project delete "$PROJECT" 2>/dev/null || true
    echo ">>> fleet destroyed"
}

cmd_shell() {
    preflight
    local name="${1:-infra}"
    exec "${LXC[@]}" exec "$name" -- bash
}

cmd_dns_check() {
    preflight
    echo ">>> DNS / reachability matrix (source: infra)"
    local ok=0
    for name in "${!CONTAINERS[@]}"; do
        [[ "$name" == "infra" ]] && continue
        if "${LXC[@]}" exec infra -- ping -c1 -W2 "$name" >/dev/null 2>&1; then
            echo "  PASS  infra -> $name"
        else
            echo "  FAIL  infra -> $name"
            ok=1
        fi
    done
    exit $ok
}

# --------------------------------------------------------------------------
# Dispatch
# --------------------------------------------------------------------------
cmd="${1:-}"
[[ -n "$cmd" ]] || { echo "usage: $0 <up|down|status|destroy|shell|dns-check> [container]"; exit 2; }

case "$cmd" in
    up) cmd_up ;;
    down) cmd_down ;;
    status) cmd_status ;;
    destroy) cmd_destroy ;;
    shell) shift; cmd_shell "$@" ;;
    dns-check) cmd_dns_check ;;
    *) die "unknown target: $cmd" ;;
esac
