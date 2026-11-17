#!/usr/bin/env bash
#
# scripts/setup_host.sh — one-shot host setup for Sentinel-GPU.
#
# Installs every host-level dependency, applies the Docker/LXD firewall
# co-existence fix, then brings the environment up. Designed for a clean
# Debian/Ubuntu-family host (Kali included); sudo required.
#
# Stages (idempotent — safe to re-run):
#   1. system packages   LXD, Terraform, AWS CLI, CUDA toolkit, build tools
#   2. firewall fix      sentinel-net forwarding + masquerade exemptions
#   3. fleet + infra     make up && make infra && make verify
#
# Usage:  sudo ./scripts/setup_host.sh [--no-fleet]
#         (fleet stage runs without root via your normal user)

set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
DO_FLEET=1
[[ "${1:-}" == "--no-fleet" ]] && DO_FLEET=0

log()  { printf '\n\033[1;36m== %s ==\033[0m\n' "$*"; }
info() { printf '  \033[0;36m>\033[0m %s\n' "$*"; }

if [[ $EUID -ne 0 ]]; then
    echo "Error: run with sudo: sudo ./scripts/setup_host.sh" >&2
    exit 1
fi

# ---------------------------------------------------------------------------
log "stage 1/3 — system packages"
# ---------------------------------------------------------------------------
if ! command -v apt-get >/dev/null 2>&1; then
    echo "Error: apt-get not found; this installer targets Debian-family hosts." >&2
    exit 1
fi

apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
    lxd python3 python3-pip python3-venv curl ca-certificates make \
    g++-14 nvidia-cuda-toolkit >/dev/null
info "LXD, build tools, CUDA toolkit installed"

# LXD snap on some distros; prefer the apt binary if it exists, else snap
if ! command -v lxc >/dev/null 2>&1 && command -v snap >/dev/null 2>&1; then
    snap install lxd || true
    info "LXD installed via snap"
fi

# LXD group membership for the invoking user (sudo may hide the real user)
REAL_USER="${SUDO_USER:-$USER}"
if ! id -nG "$REAL_USER" | grep -qw lxd; then
    usermod -aG lxd "$REAL_USER"
    info "added $REAL_USER to the 'lxd' group (re-login required)"
fi

# Terraform: official binary if apt lacks it
if ! command -v terraform >/dev/null 2>&1; then
    info "installing Terraform (hashicorp binary)"
    curl -fsSL -o /tmp/terraform.zip \
        "https://releases.hashicorp.com/terraform/1.9.8/terraform_1.9.8_linux_amd64.zip"
    unzip -o -q /tmp/terraform.zip -d /usr/local/bin
    chmod +x /usr/local/bin/terraform
    rm -f /tmp/terraform.zip
fi

# AWS CLI v2 or v1
if ! command -v aws >/dev/null 2>&1; then
    info "installing AWS CLI"
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq awscli >/dev/null 2>&1 \
        || { curl -fsSL "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" \
             -o /tmp/awscliv2.zip && \
             unzip -o -q /tmp/awscliv2.zip -d /tmp && \
             /tmp/aws/install >/dev/null && rm -rf /tmp/awscliv2.zip /tmp/aws; }
fi

# Python test deps (pip into user site of the real user)
sudo -u "$REAL_USER" python3 -m pip install --quiet --user pytest 2>/dev/null \
    || info "pytest install skipped (already present?)"

# ---------------------------------------------------------------------------
log "stage 2/3 — firewall co-existence fix (LXD + Docker)"
# ---------------------------------------------------------------------------
bash "$REPO/scripts/host-firewall.sh"

# ---------------------------------------------------------------------------
log "stage 3/3 — fleet + emulated AWS"
# ---------------------------------------------------------------------------
if [[ "$DO_FLEET" == "1" ]]; then
    # run as the real user (LXD client permissions)
    sudo -u "$REAL_USER" -E bash -c "cd '$REPO' && make up && make infra && make verify"
else
    info "skipped fleet stage (--no-fleet)"
fi

log "setup complete"
cat <<'EOF'

Next steps (as your normal user, in a fresh shell so the lxd group applies):
    make dns-check        # inter-container reachability
    make dataset          # 100 MB synthetic benchmark dataset (fixed seed)
    make test             # python test suite
    make test-cpp         # CPU/GPU kernel parity test
    make bench-kernels    # CPU vs GPU 100MB benchmark + speedup
EOF
