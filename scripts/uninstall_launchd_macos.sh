#!/usr/bin/env bash
# v0.5 — macOS launchd uninstaller for altdata-brief.
# v0.9 — also unloads the weekly digest agent installed alongside the
#        daily job.
# v0.11 — also unloads the monthly digest agent.
#
# Unloads all three LaunchAgents and removes their plists. Safe to run
# even if the agents were never installed — exits cleanly with a status
# message either way.
set -euo pipefail

LABEL="com.leonardodon.altdata-brief"
WEEKLY_LABEL="${LABEL}.weekly"
MONTHLY_LABEL="${LABEL}.monthly"
PLIST_PATH="${HOME}/Library/LaunchAgents/${LABEL}.plist"
WEEKLY_PLIST_PATH="${HOME}/Library/LaunchAgents/${WEEKLY_LABEL}.plist"
MONTHLY_PLIST_PATH="${HOME}/Library/LaunchAgents/${MONTHLY_LABEL}.plist"

if [[ "$(uname -s)" != "Darwin" ]]; then
    echo "[uninstall_launchd] this script only runs on macOS." >&2
    exit 2
fi

removed_something=0

unload_and_remove() {
    local label="$1"
    local path="$2"
    if launchctl list | grep -q "${label}"; then
        echo "[uninstall_launchd] unloading ${label} ..."
        launchctl unload "${path}" 2>/dev/null || true
        removed_something=1
    fi
    if [[ -f "${path}" ]]; then
        rm -f "${path}"
        echo "[uninstall_launchd] removed ${path}"
        removed_something=1
    fi
}

unload_and_remove "${MONTHLY_LABEL}" "${MONTHLY_PLIST_PATH}"
unload_and_remove "${WEEKLY_LABEL}" "${WEEKLY_PLIST_PATH}"
unload_and_remove "${LABEL}" "${PLIST_PATH}"

if [[ "${removed_something}" -eq 0 ]]; then
    echo "[uninstall_launchd] nothing to do — no altdata-brief LaunchAgents found."
else
    echo "[uninstall_launchd] done. Re-install any time with scripts/install_launchd_macos.sh"
fi
