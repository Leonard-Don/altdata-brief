#!/usr/bin/env bash
# v0.5 — manual run wrapper.
# v0.6 — also chains `publish` after a successful generate, gated by
#        RUN_PUBLISH_AFTER_GENERATE (default: 1).
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
#   - if generate succeeded AND RUN_PUBLISH_AFTER_GENERATE != 0,
#     chain `scripts/publish_now.sh` (which pushes the brief to
#     gh-pages so the public URL stays fresh).
#   - on non-zero exit at any stage, fire a macOS notification via osascript
#   - either way, append a one-line timestamped record to launchd_runs.log
#
# Opt-outs:
#   RUN_PUBLISH_AFTER_GENERATE=0 bash scripts/run_now.sh
#       — skip the gh-pages publish step (useful when offline)
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
    exit "${rc}"
fi

# v0.6 — chain the gh-pages publish step.
# Default ON so the daily launchd run actually keeps the public URL
# fresh. Set RUN_PUBLISH_AFTER_GENERATE=0 to opt out (e.g. when the
# laptop is offline).
if [[ "${RUN_PUBLISH_AFTER_GENERATE:-1}" != "0" ]]; then
    set +e
    bash "${PROJECT_ROOT}/scripts/publish_now.sh"
    pub_rc=$?
    set -e
    if [[ "${pub_rc}" -ne 0 ]]; then
        echo "[$(stamp)] WARN publish_now exited ${pub_rc} — brief was generated but gh-pages may be stale" \
            | tee -a "${LOG_PATH}"
        # Don't propagate the publish failure: the local generate succeeded,
        # which is the user's primary deliverable. They can rerun publish
        # manually once the network / git config is fixed.
    fi
else
    echo "[$(stamp)] publish step skipped (RUN_PUBLISH_AFTER_GENERATE=0)" | tee -a "${LOG_PATH}"
fi

exit "${rc}"
