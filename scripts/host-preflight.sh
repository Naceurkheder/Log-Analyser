#!/usr/bin/env bash
#
# scripts/host-preflight.sh
# Preflight checks for a host machine before provisioning LXD containers and
# GPU workloads.
#
# Mandatory checks: OS identification and resource retrieval.
# Optional checks : LXD server, NVIDIA tooling (missing → WARN, not FAIL).

set -o errexit
set -o nounset
set -o pipefail

# ---------------------------------------------------------------------------
# Counters
# ---------------------------------------------------------------------------
PASS_COUNT=0
WARN_COUNT=0
FAIL_COUNT=0
MANDATORY_FAILS=0

# ---------------------------------------------------------------------------
# Colors (auto-disabled when not a TTY or NO_COLOR is set)
# ---------------------------------------------------------------------------
if [[ -t 1 ]] && [[ -z "${NO_COLOR:-}" ]]; then
    C_RESET=$'\033[0m'
    C_BOLD=$'\033[1m'
    C_GREEN=$'\033[1;32m'
    C_YELLOW=$'\033[1;33m'
    C_RED=$'\033[1;31m'
    C_BLUE=$'\033[1;34m'
else
    C_RESET='' C_BOLD='' C_GREEN='' C_YELLOW='' C_RED='' C_BLUE=''
fi

# ---------------------------------------------------------------------------
# Reporting helpers (reusable PASS / WARN / FAIL)
# ---------------------------------------------------------------------------
section() {
    printf '\n%s== %s ==%s\n' "$C_BOLD$C_BLUE" "$1" "$C_RESET"
}

report_pass() {
    local name="$1"; shift
    PASS_COUNT=$((PASS_COUNT + 1))
    if (($#)); then
        printf '  %sPASS%s  %s  %s\n' "$C_GREEN" "$C_RESET" "$name" "$*"
    else
        printf '  %sPASS%s  %s\n' "$C_GREEN" "$C_RESET" "$name"
    fi
}

report_warn() {
    local name="$1"; shift
    WARN_COUNT=$((WARN_COUNT + 1))
    if (($#)); then
        printf '  %sWARN%s  %s  %s\n' "$C_YELLOW" "$C_RESET" "$name" "$*"
    else
        printf '  %sWARN%s  %s\n' "$C_YELLOW" "$C_RESET" "$name"
    fi
}

report_fail() {
    local name="$1"; shift
    FAIL_COUNT=$((FAIL_COUNT + 1))
    MANDATORY_FAILS=$((MANDATORY_FAILS + 1))
    if (($#)); then
        printf '  %sFAIL%s  %s  %s\n' "$C_RED" "$C_RESET" "$name" "$*"
    else
        printf '  %sFAIL%s  %s\n' "$C_RED" "$C_RESET" "$name"
    fi
}

# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------
have_cmd() {
    command -v -- "$1" >/dev/null 2>&1
}

bytes_to_human() {
    local bytes="$1"
    awk -v b="$bytes" 'BEGIN {
        split("B KiB MiB GiB TiB PiB", u, " ");
        i = 1;
        while (b >= 1024 && i < 6) { b /= 1024; i++ }
        printf "%.2f %s", b, u[i]
    }'
}

# Extract a KEY=value from /etc/os-release without sourcing it.
get_os_field() {
    local file="$1" key="$2" line value
    line="$(grep -m1 "^${key}=" "$file" 2>/dev/null)" || true
    if [[ -z "${line:-}" ]]; then
        return 0
    fi
    value="${line#*=}"
    value="${value%\"}"; value="${value#\"}"
    value="${value%\'}"; value="${value#\'}"
    printf '%s' "$value"
}

# ---------------------------------------------------------------------------
# Section B: Operating system
# ---------------------------------------------------------------------------
check_os() {
    section "Operating system"

    local os_release="/etc/os-release"
    if [[ ! -r "$os_release" ]]; then
        report_fail "os-release" "cannot read $os_release"
        return 0
    fi
    report_pass "os-release" "$os_release is readable"

    local name version id version_id kernel
    name="$(get_os_field       "$os_release" NAME)"
    version="$(get_os_field    "$os_release" VERSION)"
    id="$(get_os_field         "$os_release" ID)"
    version_id="$(get_os_field "$os_release" VERSION_ID)"

    if [[ -n "$name" ]]; then
        report_pass "Distribution" "$name${version:+ ($version)}"
    else
        report_fail "Distribution" "NAME field missing"
    fi

    if [[ -n "$id" && -n "$version_id" ]]; then
        report_pass "Distribution ID" "$id $version_id"
    else
        report_warn "Distribution ID" "ID/VERSION_ID incomplete"
    fi

    if kernel="$(uname -r 2>/dev/null)" && [[ -n "$kernel" ]]; then
        report_pass "Kernel" "$kernel"
    else
        report_fail "Kernel" "uname -r failed"
    fi
}

# ---------------------------------------------------------------------------
# Section C: Resources
# ---------------------------------------------------------------------------
check_resources() {
    section "Resources"

    if [[ -r /proc/meminfo ]]; then
        local total_kb avail_kb swap_kb
        total_kb="$(awk '/^MemTotal:/     {print $2}' /proc/meminfo)"
        avail_kb="$(awk '/^MemAvailable:/ {print $2}' /proc/meminfo)"
        swap_kb="$(awk  '/^SwapTotal:/    {print $2}' /proc/meminfo)"

        if [[ -n "$total_kb" ]]; then
            report_pass "Total RAM"     "$(bytes_to_human "$((total_kb * 1024))")"
        else
            report_fail "Total RAM"     "MemTotal not found"
        fi

        if [[ -n "$avail_kb" ]]; then
            report_pass "Available RAM" "$(bytes_to_human "$((avail_kb * 1024))")"
        else
            report_fail "Available RAM" "MemAvailable not found"
        fi

        if [[ -n "$swap_kb" ]]; then
            report_pass "Swap"          "$(bytes_to_human "$((swap_kb * 1024))")"
        else
            report_warn "Swap"          "SwapTotal not found"
        fi
    else
        report_fail "Memory" "/proc/meminfo is not readable"
    fi

    local cpus=""
    if have_cmd nproc; then
        cpus="$(nproc 2>/dev/null || true)"
    fi
    if [[ -z "$cpus" ]]; then
        cpus="$(getconf _NPROCESSORS_ONLN 2>/dev/null || true)"
    fi
    if [[ -n "$cpus" ]]; then
        report_pass "Logical CPUs" "$cpus"
    else
        report_fail "Logical CPUs" "could not determine CPU count"
    fi

    local avail_disk_kb
    avail_disk_kb="$(df -Pk / 2>/dev/null | awk 'NR==2 {print $4}')" || true
    if [[ -n "${avail_disk_kb:-}" ]]; then
        report_pass "Available /" "$(bytes_to_human "$((avail_disk_kb * 1024))")"
    else
        report_fail "Available /" "df could not read filesystem"
    fi
}

# ---------------------------------------------------------------------------
# Section D: Infrastructure (optional — WARN on absence, never FAIL)
# ---------------------------------------------------------------------------
check_infrastructure() {
    section "Infrastructure"

    # --- LXD ---
    if have_cmd lxc; then
        report_pass "lxc" "$(command -v lxc)"
        if timeout 10 lxc info >/dev/null 2>&1; then
            report_pass "LXD server" "lxc info succeeded"
        else
            report_warn "LXD server" "lxc info failed or timed out"
        fi
    else
        report_warn "lxc"        "command not found in PATH"
        report_warn "LXD server" "skipped (lxc unavailable)"
    fi

    # --- NVIDIA ---
    if have_cmd nvidia-smi; then
        report_pass "nvidia-smi" "$(command -v nvidia-smi)"
        if timeout 10 nvidia-smi >/dev/null 2>&1; then
            report_pass "nvidia-smi execution" "returned success"
        else
            report_warn "nvidia-smi execution" "returned non-zero"
        fi
    else
        report_warn "nvidia-smi" "command not found in PATH"
    fi

    if have_cmd nvcc; then
        report_pass "nvcc" "$(command -v nvcc)"
    else
        report_warn "nvcc" "command not found in PATH"
    fi
}

# ---------------------------------------------------------------------------
# Section E: Summary
# ---------------------------------------------------------------------------
summary() {
    section "Summary"
    printf '  PASS: %d\n' "$PASS_COUNT"
    printf '  WARN: %d\n' "$WARN_COUNT"
    printf '  FAIL: %d\n' "$FAIL_COUNT"

    if (( MANDATORY_FAILS > 0 )); then
        printf '  %sRESULT: FAIL%s (%d mandatory failure(s))\n' \
            "$C_RED" "$C_RESET" "$MANDATORY_FAILS"
        return 1
    fi
    printf '  %sRESULT: PASS%s\n' "$C_GREEN" "$C_RESET"
    return 0
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
main() {
    printf '%sHost preflight%s — %s\n' \
        "$C_BOLD" "$C_RESET" "$(hostname 2>/dev/null || echo unknown)"

    check_os
    check_resources
    check_infrastructure

    if summary; then
        exit 0
    else
        exit 1
    fi
}

main "$@"
