
#!/usr/bin/env bash
# launch-dev.sh — Idempotently ensure an LXD dev container exists and is running.
#
# Usage:
#   ./launch-dev.sh [INSTANCE_NAME] [IMAGE]
#
# Defaults:
#   INSTANCE_NAME = dev-container
#   IMAGE         = ubuntu:22.04
#
# Behaviour:
#   - Verifies the LXD client and daemon are reachable.
#   - Creates the instance from IMAGE if it does not exist.
#   - Starts the instance if it exists but is stopped.
#   - Leaves a running instance untouched.
#   - Never deletes or recreates an existing instance.
#   - Safe to run repeatedly (idempotent).

set -euo pipefail

INSTANCE_NAME="${1:-sentinel-dev}"
IMAGE="${2:-ubuntu:22.04}"

if [ -z "$INSTANCE_NAME" ]; then
    echo "Error: INSTANCE_NAME must not be empty." >&2
    exit 1
fi

# --- 1. Verify LXD is available ---------------------------------------------
if ! command -v lxc >/dev/null 2>&1; then
    echo "Error: 'lxc' command not found. Please install LXD first." >&2
    exit 1
fi

if ! lxc info >/dev/null 2>&1; then
    echo "Error: cannot reach the LXD daemon. Is it running and are you in the 'lxd' group?" >&2
    exit 1
fi

# --- 2. Does the instance already exist? ------------------------------------
if lxc info "$INSTANCE_NAME" >/dev/null 2>&1; then
    echo "Instance '$INSTANCE_NAME' already exists."

    # Parse current status from `lxc info` (e.g. "Status: RUNNING").
    state="$(lxc info "$INSTANCE_NAME" \
        | awk -F': *' '/^Status:/ {print $2; exit}')"

    case "$state" in
        RUNNING)
            echo "Instance is already running — nothing to do."
            ;;
        STOPPED|FROZEN)
            echo "Instance is $state; starting it..."
            lxc start "$INSTANCE_NAME"
            echo "Instance '$INSTANCE_NAME' started."
            ;;
        *)
            echo "Error: unexpected instance state '$state'; refusing to act." >&2
            exit 1
            ;;
    esac
else
    # --- 3. Create the instance ---------------------------------------------
    echo "Creating instance '$INSTANCE_NAME' from image '$IMAGE'..."
    lxc launch "$IMAGE" "$INSTANCE_NAME"
    echo "Instance '$INSTANCE_NAME' created and started."
fi
