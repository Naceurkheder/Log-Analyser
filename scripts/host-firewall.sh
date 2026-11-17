#!/usr/bin/env bash
#
# scripts/host-firewall.sh — Host-side firewall exemptions for the fleet bridge.
#
# Docker installs nftables base chains (policy DROP on FORWARD) that shadow
# LXD's NAT for managed bridges. Exempting the fleet bridge in DOCKER-USER is
# the documented co-existence fix; it leaves Docker's own traffic untouched.
#
# Requires root. Safe to run repeatedly (idempotent).

set -euo pipefail

NET="${SENTINEL_NET:-sentinel-net}"
CIDR="${SENTINEL_NET_CIDR:-10.20.0.1/24}"

[[ $EUID -eq 0 ]] || { echo "Error: run with sudo" >&2; exit 1; }
command -v iptables >/dev/null 2>&1 || { echo "Error: iptables not found" >&2; exit 1; }

rule_exists() { iptables -C "$@" 2>/dev/null; }

# 1. Forwarding exemptions via Docker's user hook (evaluated first in FORWARD).
for rule in "-i $NET -j ACCEPT" "-o $NET -j ACCEPT"; do
    if rule_exists DOCKER-USER $rule 2>/dev/null; then
        echo "  present  DOCKER-USER $rule"
    else
        iptables -I DOCKER-USER $rule && echo "  added    DOCKER-USER $rule"
    fi
done

# 2. Masquerade fleet traffic leaving the host (no-op if LXD already installed one).
if rule_exists -t nat POSTROUTING -s "$CIDR" -j MASQUERADE 2>/dev/null; then
    echo "  present  nat/POSTROUTING MASQUERADE for $CIDR"
else
    iptables -t nat -I POSTROUTING -s "$CIDR" -j MASQUERADE \
        && echo "  added    nat/POSTROUTING MASQUERADE for $CIDR"
fi

# 3. IP forwarding must be on (LXD normally enables it; assert anyway).
if [[ "$(cat /proc/sys/net/ipv4/ip_forward)" != "1" ]]; then
    echo 1 > /proc/sys/net/ipv4/ip_forward
    echo "  enabled  ip_forward"
fi

echo "Firewall exemptions applied for bridge '$NET'."
echo "Note: iptables rules are not persisted across reboots on this host — re-run this script (or the installer) after reboot."
