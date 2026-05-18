#!/usr/bin/env bash
# v0.11 — monthly digest wrapper script.
#
# Runs the same command the first-of-month launchd job runs, so the
# setup can be validated without waiting for the 1st. Mirrors the
# shape of weekly_digest_now.sh:
#   - cd to project root
#   - run `uv sync --quiet`
#   - run `uv run cn-altdata-brief monthly-digest`
#   - if digest succeeded AND RUN_PUBLISH_AFTER_DIGEST != 0, chain
#     `scripts/publish_now.sh` so the monthly digest lands on gh-pages.
#   - on non-zero exit at any stage, fire a macOS notification via osascript
#   - either way, append a one-line timestamped record to launchd_runs.log
#
# Weekend / holiday handling:
#   By default, when launchd fires on the 1st-of-month and that day is
#   Sat / Sun, the wrapper DEFERS the run until the next Mon and exits 0.
#   This avoids generating a monthly digest while half the team is OOO.
#   Override with MONTHLY_DEFER_WEEKENDS=0 to always run on day-1.
#
# Opt-outs:
#   RUN_PUBLISH_AFTER_DIGEST=0 bash scripts/monthly_digest_now.sh
#       — skip the gh-pages publish step (useful when offline)
#   MONTHLY_OF=2026-04 bash scripts/monthly_digest_now.sh
#       — back-fill last month's monthly digest
#   MONTHLY_DEFER_WEEKENDS=0 bash scripts/monthly_digest_now.sh
#       — don't defer to Monday even if today is Sat / Sun
set -uo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG_PATH="${PROJECT_ROOT}/output/launchd_runs.log"
mkdir -p "${PROJECT_ROOT}/output"

cd "${PROJECT_ROOT}"

stamp() { date "+%Y-%m-%dT%H:%M:%S%z"; }

echo "[$(stamp)] monthly_digest_now START (pwd=${PROJECT_ROOT})" | tee -a "${LOG_PATH}"

# Weekend deferral. We only defer when the user explicitly DIDN'T set
# MONTHLY_OF (i.e. this is the natural 1st-of-month run, not a backfill).
if [[ "${MONTHLY_OF:-}" == "" && "${MONTHLY_DEFER_WEEKENDS:-1}" != "0" ]]; then
    dow=$(date +%u)  # 1=Mon..7=Sun
    if [[ "${dow}" -ge 6 ]]; then
        echo "[$(stamp)] today is weekend (dow=${dow}); deferring monthly digest to next Monday." \
            | tee -a "${LOG_PATH}"
        exit 0
    fi
fi

if ! command -v uv >/dev/null 2>&1; then
    msg="uv not found in PATH; cn-altdata-brief monthly-digest cannot proceed"
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
if [[ "${MONTHLY_OF:-}" != "" ]]; then
    extra_args+=("--month-of" "${MONTHLY_OF}")
fi

set +e
uv run cn-altdata-brief monthly-digest "${extra_args[@]}" >>"${LOG_PATH}" 2>&1
rc=$?
set -e

if [[ "${rc}" -eq 0 ]]; then
    echo "[$(stamp)] monthly_digest_now OK (exit=0)" | tee -a "${LOG_PATH}"
else
    msg="monthly-digest failed (exit=${rc}); see ${LOG_PATH}"
    echo "[$(stamp)] ERROR ${msg}" | tee -a "${LOG_PATH}"
    if [[ "$(uname -s)" == "Darwin" ]]; then
        osascript -e "display notification \"${msg}\" with title \"cn-altdata-brief\"" || true
    fi
    exit "${rc}"
fi

# Chain the publish step so the new monthly digest reaches gh-pages.
if [[ "${RUN_PUBLISH_AFTER_DIGEST:-1}" != "0" ]]; then
    set +e
    bash "${PROJECT_ROOT}/scripts/publish_now.sh"
    pub_rc=$?
    set -e
    if [[ "${pub_rc}" -ne 0 ]]; then
        echo "[$(stamp)] WARN publish_now exited ${pub_rc} — monthly digest was generated but gh-pages may be stale" \
            | tee -a "${LOG_PATH}"
    fi
else
    echo "[$(stamp)] publish step skipped (RUN_PUBLISH_AFTER_DIGEST=0)" | tee -a "${LOG_PATH}"
fi

exit "${rc}"
