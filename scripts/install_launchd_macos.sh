#!/usr/bin/env bash
# v0.5 — macOS launchd installer for cn-altdata-brief
#
# Installs a LaunchAgent that runs `uv run cn-altdata-brief generate
# --source-mode auto` on weekdays at 17:00 (local time, which on a
# CST/UTC+8 box equals 17:00 Beijing time — after market close).
#
# The job appends to `output/launchd_runs.log` and fires a macOS
# notification when the run exits non-zero.
#
# Usage:
#   bash scripts/install_launchd_macos.sh
#
# Idempotent: re-running rewrites the plist and reloads the agent.
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LABEL="com.leonardodon.cn-altdata-brief"
PLIST_DIR="${HOME}/Library/LaunchAgents"
PLIST_PATH="${PLIST_DIR}/${LABEL}.plist"
LOG_PATH="${PROJECT_ROOT}/output/launchd_runs.log"
RUN_WRAPPER="${PROJECT_ROOT}/scripts/run_now.sh"

if [[ "$(uname -s)" != "Darwin" ]]; then
    echo "[install_launchd] this script only runs on macOS. For Linux, use cron (see README)." >&2
    exit 2
fi

if ! command -v uv >/dev/null 2>&1; then
    echo "[install_launchd] uv not found in PATH; install uv first: https://docs.astral.sh/uv/" >&2
    exit 2
fi
UV_BIN="$(command -v uv)"

mkdir -p "${PLIST_DIR}"
mkdir -p "${PROJECT_ROOT}/output"

# Resolve a sensible PATH for the launchd-managed shell, since the
# graphical-session launchd inherits a much skinnier PATH than your tty.
LAUNCHD_PATH="$(dirname "${UV_BIN}"):/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"

cat > "${PLIST_PATH}" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
  <dict>
    <key>Label</key>
    <string>${LABEL}</string>
    <key>ProgramArguments</key>
    <array>
      <string>/bin/bash</string>
      <string>${RUN_WRAPPER}</string>
    </array>
    <key>WorkingDirectory</key>
    <string>${PROJECT_ROOT}</string>
    <key>EnvironmentVariables</key>
    <dict>
      <key>PATH</key>
      <string>${LAUNCHD_PATH}</string>
    </dict>
    <key>StartCalendarInterval</key>
    <array>
      <dict>
        <key>Weekday</key>
        <integer>1</integer>
        <key>Hour</key>
        <integer>17</integer>
        <key>Minute</key>
        <integer>0</integer>
      </dict>
      <dict>
        <key>Weekday</key>
        <integer>2</integer>
        <key>Hour</key>
        <integer>17</integer>
        <key>Minute</key>
        <integer>0</integer>
      </dict>
      <dict>
        <key>Weekday</key>
        <integer>3</integer>
        <key>Hour</key>
        <integer>17</integer>
        <key>Minute</key>
        <integer>0</integer>
      </dict>
      <dict>
        <key>Weekday</key>
        <integer>4</integer>
        <key>Hour</key>
        <integer>17</integer>
        <key>Minute</key>
        <integer>0</integer>
      </dict>
      <dict>
        <key>Weekday</key>
        <integer>5</integer>
        <key>Hour</key>
        <integer>17</integer>
        <key>Minute</key>
        <integer>0</integer>
      </dict>
    </array>
    <key>StandardOutPath</key>
    <string>${LOG_PATH}</string>
    <key>StandardErrorPath</key>
    <string>${LOG_PATH}</string>
    <key>RunAtLoad</key>
    <false/>
    <key>ProcessType</key>
    <string>Background</string>
  </dict>
</plist>
PLIST

echo "[install_launchd] wrote plist -> ${PLIST_PATH}"

# Reload (unload then load) so an existing job picks up the new plist.
if launchctl list | grep -q "${LABEL}"; then
    echo "[install_launchd] unloading existing job ..."
    launchctl unload "${PLIST_PATH}" 2>/dev/null || true
fi

echo "[install_launchd] loading job ..."
launchctl load "${PLIST_PATH}"

if launchctl list | grep -q "${LABEL}"; then
    echo "[install_launchd] OK — job is queued. Verify with:"
    echo "    launchctl list | grep cn-altdata"
    echo "    tail -f ${LOG_PATH}"
    echo
    echo "Manual test (no need to wait for 17:00):"
    echo "    bash ${RUN_WRAPPER}"
else
    echo "[install_launchd] WARNING: launchctl list did not show the label; check Console.app for errors." >&2
    exit 3
fi
