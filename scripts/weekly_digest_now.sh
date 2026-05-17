#!/usr/bin/env bash
# v0.9 — weekly digest wrapper script.
#
# Runs the same command the Friday launchd job runs, so the setup can
# be validated without waiting for 18:00. Mirrors the shape of
# run_now.sh / publish_now.sh:
#   - cd to project root
#   - run `uv sync --quiet`
#   - run `uv run cn-altdata-brief weekly-digest`
#   - if digest succeeded AND RUN_PUBLISH_AFTER_DIGEST != 0, chain
#     `scripts/publish_now.sh` so the digest lands on gh-pages.
#   - on non-zero exit at any stage, fire a macOS notification via osascript
#   - either way, append a one-line timestamped record to launchd_runs.log
#
# Opt-outs:
#   RUN_PUBLISH_AFTER_DIGEST=0 bash scripts/weekly_digest_now.sh
#       — skip the gh-pages publish step (useful when offline)
#   DIGEST_WEEK_OF=2026-05-14 bash scripts/weekly_digest_now.sh
#       — generate a back-dated digest for the week containing that date
set -uo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG_PATH="${PROJECT_ROOT}/output/launchd_runs.log"
mkdir -p "${PROJECT_ROOT}/output"

cd "${PROJECT_ROOT}"

stamp() { date "+%Y-%m-%dT%H:%M:%S%z"; }

echo "[$(stamp)] weekly_digest_now START (pwd=${PROJECT_ROOT})" | tee -a "${LOG_PATH}"

if ! command -v uv >/dev/null 2>&1; then
    msg="uv not found in PATH; cn-altdata-brief weekly-digest cannot proceed"
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

extra_args=()
if [[ "${DIGEST_WEEK_OF:-}" != "" ]]; then
    extra_args+=("--week-of" "${DIGEST_WEEK_OF}")
fi

set +e
uv run cn-altdata-brief weekly-digest "${extra_args[@]}" >>"${LOG_PATH}" 2>&1
rc=$?
set -e

if [[ "${rc}" -eq 0 ]]; then
    echo "[$(stamp)] weekly_digest_now OK (exit=0)" | tee -a "${LOG_PATH}"
else
    msg="weekly-digest failed (exit=${rc}); see ${LOG_PATH}"
    echo "[$(stamp)] ERROR ${msg}" | tee -a "${LOG_PATH}"
    if [[ "$(uname -s)" == "Darwin" ]]; then
        osascript -e "display notification \"${msg}\" with title \"cn-altdata-brief\"" || true
    fi
    exit "${rc}"
fi

# Chain the publish step so the new digest reaches gh-pages without
# requiring a second manual command. The Friday daily run is expected
# to have already published the Friday brief earlier in the day, so
# this publish call is idempotent for the daily content and additive
# for the new weekly digest.
if [[ "${RUN_PUBLISH_AFTER_DIGEST:-1}" != "0" ]]; then
    set +e
    bash "${PROJECT_ROOT}/scripts/publish_now.sh"
    pub_rc=$?
    set -e
    if [[ "${pub_rc}" -ne 0 ]]; then
        echo "[$(stamp)] WARN publish_now exited ${pub_rc} — digest was generated but gh-pages may be stale" \
            | tee -a "${LOG_PATH}"
    fi
else
    echo "[$(stamp)] publish step skipped (RUN_PUBLISH_AFTER_DIGEST=0)" | tee -a "${LOG_PATH}"
fi

exit "${rc}"
