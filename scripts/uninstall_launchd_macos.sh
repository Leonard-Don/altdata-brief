#!/usr/bin/env bash
# v0.5 — macOS launchd uninstaller for cn-altdata-brief.
#
# Unloads the LaunchAgent and removes its plist. Safe to run even if
# the agent was never installed — exits cleanly with a status message.
set -euo pipefail

LABEL="com.leonardodon.cn-altdata-brief"
PLIST_PATH="${HOME}/Library/LaunchAgents/${LABEL}.plist"

if [[ "$(uname -s)" != "Darwin" ]]; then
    echo "[uninstall_launchd] this script only runs on macOS." >&2
    exit 2
fi

removed_something=0

if launchctl list | grep -q "${LABEL}"; then
    echo "[uninstall_launchd] unloading ${LABEL} ..."
    launchctl unload "${PLIST_PATH}" 2>/dev/null || true
    removed_something=1
fi

if [[ -f "${PLIST_PATH}" ]]; then
    rm -f "${PLIST_PATH}"
    echo "[uninstall_launchd] removed ${PLIST_PATH}"
    removed_something=1
fi

if [[ "${removed_something}" -eq 0 ]]; then
    echo "[uninstall_launchd] nothing to do — no LaunchAgent named ${LABEL} found."
else
    echo "[uninstall_launchd] done. Re-install any time with scripts/install_launchd_macos.sh"
fi
