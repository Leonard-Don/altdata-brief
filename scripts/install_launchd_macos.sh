#!/usr/bin/env bash
# v0.5 — macOS launchd installer for cn-altdata-brief
# v0.9 — adds a second LaunchAgent for the Friday weekly digest.
#
# Installs two LaunchAgents:
#
#   1. com.leonardodon.cn-altdata-brief         — Mon-Fri 17:00 daily
#      brief (`uv run cn-altdata-brief generate --source-mode auto`).
#   2. com.leonardodon.cn-altdata-brief.weekly  — Friday 18:00 weekly
#      digest (`uv run cn-altdata-brief weekly-digest`), an hour after
#      the daily run so all five briefs already exist on disk.
#
# Both jobs append to `output/launchd_runs.log` and fire a macOS
# notification when the run exits non-zero.
#
# Usage:
#   bash scripts/install_launchd_macos.sh
#
# Idempotent: re-running rewrites both plists and reloads each agent.
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LABEL="com.leonardodon.cn-altdata-brief"
WEEKLY_LABEL="${LABEL}.weekly"
PLIST_DIR="${HOME}/Library/LaunchAgents"
PLIST_PATH="${PLIST_DIR}/${LABEL}.plist"
WEEKLY_PLIST_PATH="${PLIST_DIR}/${WEEKLY_LABEL}.plist"
LOG_PATH="${PROJECT_ROOT}/output/launchd_runs.log"
RUN_WRAPPER="${PROJECT_ROOT}/scripts/run_now.sh"
WEEKLY_WRAPPER="${PROJECT_ROOT}/scripts/weekly_digest_now.sh"

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

# v0.9 — Friday 18:00 weekly digest plist. The hour-of-margin between
# the 17:00 daily brief and the 18:00 digest gives the daily pipeline
# time to write the Friday brief before the digest aggregates it.
cat > "${WEEKLY_PLIST_PATH}" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
  <dict>
    <key>Label</key>
    <string>${WEEKLY_LABEL}</string>
    <key>ProgramArguments</key>
    <array>
      <string>/bin/bash</string>
      <string>${WEEKLY_WRAPPER}</string>
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
        <integer>5</integer>
        <key>Hour</key>
        <integer>18</integer>
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

echo "[install_launchd] wrote plist -> ${WEEKLY_PLIST_PATH}"

# Reload (unload then load) so an existing job picks up the new plist.
if launchctl list | grep -q "${LABEL}\b"; then
    echo "[install_launchd] unloading existing daily job ..."
    launchctl unload "${PLIST_PATH}" 2>/dev/null || true
fi
if launchctl list | grep -q "${WEEKLY_LABEL}"; then
    echo "[install_launchd] unloading existing weekly job ..."
    launchctl unload "${WEEKLY_PLIST_PATH}" 2>/dev/null || true
fi

echo "[install_launchd] loading daily job ..."
launchctl load "${PLIST_PATH}"
echo "[install_launchd] loading weekly job ..."
launchctl load "${WEEKLY_PLIST_PATH}"

ok_daily=0
ok_weekly=0
if launchctl list | grep -q "${LABEL}\b"; then ok_daily=1; fi
if launchctl list | grep -q "${WEEKLY_LABEL}"; then ok_weekly=1; fi

if [[ "${ok_daily}" -eq 1 && "${ok_weekly}" -eq 1 ]]; then
    echo "[install_launchd] OK — both jobs are queued. Verify with:"
    echo "    launchctl list | grep cn-altdata"
    echo "    tail -f ${LOG_PATH}"
    echo
    echo "Manual tests (no need to wait for 17:00 / Friday 18:00):"
    echo "    bash ${RUN_WRAPPER}            # daily brief"
    echo "    bash ${WEEKLY_WRAPPER}         # weekly digest"
else
    echo "[install_launchd] WARNING: launchctl list did not show one or both labels; check Console.app for errors." >&2
    exit 3
fi
