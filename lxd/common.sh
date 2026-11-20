#!/usr/bin/env bash
#
# lxd/common.sh — shared LXD configuration for the helper scripts.
#
# Source it (do not execute):  . "$(dirname "$0")/common.sh"
#
# Keeping the project name here prevents the class of bug where one helper
# talks to a different LXD project than the fleet (the containers then look
# "missing" even though they are running).

PROJECT="${LXD_PROJECT:-sentinel}"
LXC=(lxc --project "$PROJECT")

lxd_require() {
    command -v lxc >/dev/null 2>&1 || {
        echo "Error: 'lxc' not found. Install LXD first." >&2
        return 1
    }
    lxc info >/dev/null 2>&1 || {
        echo "Error: cannot reach the LXD daemon (running? in 'lxd' group?)" >&2
        return 1
    }
}

lxd_instance_exists() {
    "${LXC[@]}" info "$1" >/dev/null 2>&1
}

lxd_exec() {
    "${LXC[@]}" exec "$1" -- "${@:2}"
}
