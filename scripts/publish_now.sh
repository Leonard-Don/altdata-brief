#!/usr/bin/env bash
# v0.6 — manual publish wrapper.
#
# Pushes the most recent brief to the gh-pages branch and updates the
# public landing page. Mirrors the shape of run_now.sh:
#   - cd to project root
#   - uv sync --quiet
#   - uv run altdata-brief publish (honors PUBLISH_EXTRA_ARGS env)
#   - log to output/launchd_runs.log
#   - macOS notification on failure
#
# Useful overrides:
#   PUBLISH_DRY_RUN=1 bash scripts/publish_now.sh
#       — show the plan, do not touch git
#   PUBLISH_NO_PUSH=1 bash scripts/publish_now.sh
#       — commit to gh-pages locally, skip `git push`
#   PUBLISH_DATE=2026-05-17 bash scripts/publish_now.sh
#       — publish a specific past brief
set -uo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG_PATH="${PROJECT_ROOT}/output/launchd_runs.log"
mkdir -p "${PROJECT_ROOT}/output"

cd "${PROJECT_ROOT}"

stamp() { date "+%Y-%m-%dT%H:%M:%S%z"; }

echo "[$(stamp)] publish_now START (pwd=${PROJECT_ROOT})" | tee -a "${LOG_PATH}"

if ! command -v uv >/dev/null 2>&1; then
    msg="uv not found in PATH; altdata-brief publish cannot proceed"
    echo "[$(stamp)] ERROR ${msg}" | tee -a "${LOG_PATH}"
    if [[ "$(uname -s)" == "Darwin" ]]; then
        osascript -e "display notification \"${msg}\" with title \"altdata-brief\"" || true
    fi
    exit 99
fi

uv sync --quiet >>"${LOG_PATH}" 2>&1 || {
    msg="uv sync failed; see ${LOG_PATH}"
    echo "[$(stamp)] ERROR ${msg}" | tee -a "${LOG_PATH}"
    if [[ "$(uname -s)" == "Darwin" ]]; then
        osascript -e "display notification \"${msg}\" with title \"altdata-brief\"" || true
    fi
    exit 1
}

# Build the argv from env flags so callers can opt into dry-run / no-push
# without forking the script.
extra_args=()
if [[ "${PUBLISH_DATE:-}" != "" ]]; then
    extra_args+=("--date" "${PUBLISH_DATE}")
fi
if [[ "${PUBLISH_DRY_RUN:-0}" == "1" ]]; then
    extra_args+=("--dry-run")
fi
if [[ "${PUBLISH_NO_PUSH:-0}" == "1" ]]; then
    extra_args+=("--no-push")
fi
if [[ "${PUBLISH_GH_PAGES_BRANCH:-}" != "" ]]; then
    extra_args+=("--gh-pages-branch" "${PUBLISH_GH_PAGES_BRANCH}")
fi

set +e
uv run altdata-brief publish "${extra_args[@]}" >>"${LOG_PATH}" 2>&1
rc=$?
set -e

if [[ "${rc}" -eq 0 ]]; then
    echo "[$(stamp)] publish_now OK (exit=0)" | tee -a "${LOG_PATH}"
else
    msg="publish failed (exit=${rc}); see ${LOG_PATH}"
    echo "[$(stamp)] ERROR ${msg}" | tee -a "${LOG_PATH}"
    if [[ "$(uname -s)" == "Darwin" ]]; then
        osascript -e "display notification \"${msg}\" with title \"altdata-brief\"" || true
    fi
fi

exit "${rc}"
