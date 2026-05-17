#!/usr/bin/env bash
# v0.5 — manual run wrapper.
#
# Runs the same command the launchd job runs, so you can validate the
# setup without waiting for 17:00. Also doubles as the launchd
# ProgramArguments entry point — that way installer/runtime/manual all
# share one code path.
#
# Behaviour:
#   - cd to project root
#   - run `uv sync --quiet` (cheap idempotent step)
#   - run `uv run cn-altdata-brief generate --source-mode auto`
#   - on non-zero exit, fire a macOS notification via osascript
#   - either way, append a one-line timestamped record to launchd_runs.log
set -uo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG_PATH="${PROJECT_ROOT}/output/launchd_runs.log"
mkdir -p "${PROJECT_ROOT}/output"

cd "${PROJECT_ROOT}"

stamp() { date "+%Y-%m-%dT%H:%M:%S%z"; }

echo "[$(stamp)] run_now START (pwd=${PROJECT_ROOT})" | tee -a "${LOG_PATH}"

if ! command -v uv >/dev/null 2>&1; then
    msg="uv not found in PATH; cn-altdata-brief daily run cannot proceed"
    echo "[$(stamp)] ERROR ${msg}" | tee -a "${LOG_PATH}"
    if [[ "$(uname -s)" == "Darwin" ]]; then
        osascript -e "display notification \"${msg}\" with title \"cn-altdata-brief\"" || true
    fi
    exit 99
fi

uv sync --quiet >>"${LOG_PATH}" 2>&1 || {
    msg="uv sync failed; see ${LOG_PATH}"
    echo "[$(stamp)] ERROR ${msg}" | tee -a "${LOG_PATH}"
    if [[ "$(uname -s)" == "Darwin" ]]; then
        osascript -e "display notification \"${msg}\" with title \"cn-altdata-brief\"" || true
    fi
    exit 1
}

set +e
uv run cn-altdata-brief generate --source-mode auto >>"${LOG_PATH}" 2>&1
rc=$?
set -e

if [[ "${rc}" -eq 0 ]]; then
    echo "[$(stamp)] run_now OK (exit=0)" | tee -a "${LOG_PATH}"
else
    msg="generate failed (exit=${rc}); see ${LOG_PATH}"
    echo "[$(stamp)] ERROR ${msg}" | tee -a "${LOG_PATH}"
    if [[ "$(uname -s)" == "Darwin" ]]; then
        osascript -e "display notification \"${msg}\" with title \"cn-altdata-brief\"" || true
    fi
fi

exit "${rc}"
