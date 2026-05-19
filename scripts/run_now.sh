#!/usr/bin/env bash
# v0.5 — manual run wrapper.
# v0.6 — also chains `publish` after a successful generate, gated by
#        RUN_PUBLISH_AFTER_GENERATE (default: 1).
# v0.12 — optional content-quality pre-publish guard. When
#         CN_ALTDATA_BRIEF_STRICT=1, runs `validate --strict` after
#         generate; a FAIL aborts the publish step (the locally generated
#         brief is still kept on disk for inspection).
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
#   - if CN_ALTDATA_BRIEF_STRICT=1, run `validate --strict` as a
#     pre-publish guard. Exit code 2 (any FAIL) aborts the publish step.
#   - if generate succeeded AND RUN_PUBLISH_AFTER_GENERATE != 0 AND the
#     strict guard didn't block, chain `scripts/publish_now.sh` (which
#     pushes the brief to gh-pages so the public URL stays fresh).
#   - on non-zero exit at any stage, fire a macOS notification via osascript
#   - either way, append a one-line timestamped record to launchd_runs.log
#
# Opt-outs / opt-ins:
#   RUN_PUBLISH_AFTER_GENERATE=0 bash scripts/run_now.sh
#       — skip the gh-pages publish step (useful when offline)
#   CN_ALTDATA_BRIEF_STRICT=1 bash scripts/run_now.sh
#       — enable the v0.12 content-quality strict guard
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

# v0.12 — pre-publish content-quality guard.
# When CN_ALTDATA_BRIEF_STRICT=1, run the v0.12 quality checks with
# --fail-on-warn. Any non-zero exit (WARN, FAIL, or validator runtime
# failure) aborts the publish step so we don't ship a brief whose inputs
# failed structural / content sanity. The locally generated brief is kept
# on disk so the operator can inspect and decide.
strict_block_publish=0
if [[ "${CN_ALTDATA_BRIEF_STRICT:-0}" == "1" ]]; then
    set +e
    uv run cn-altdata-brief validate --strict --fail-on-warn >>"${LOG_PATH}" 2>&1
    strict_rc=$?
    set -e
    if [[ "${strict_rc}" -ne 0 ]]; then
        msg="validate --strict FAILED (exit=${strict_rc}); aborting publish, brief kept on disk"
        echo "[$(stamp)] ERROR ${msg}" | tee -a "${LOG_PATH}"
        if [[ "$(uname -s)" == "Darwin" ]]; then
            osascript -e "display notification \"${msg}\" with title \"cn-altdata-brief\"" || true
        fi
        strict_block_publish=1
    else
        echo "[$(stamp)] strict guard OK (exit=0)" | tee -a "${LOG_PATH}"
    fi
fi

# v0.6 — chain the gh-pages publish step.
# Default ON so the daily launchd run actually keeps the public URL
# fresh. Set RUN_PUBLISH_AFTER_GENERATE=0 to opt out (e.g. when the
# laptop is offline). v0.12 strict-FAIL also short-circuits the publish.
if [[ "${strict_block_publish}" == "1" ]]; then
    echo "[$(stamp)] publish step skipped (strict guard blocked)" | tee -a "${LOG_PATH}"
elif [[ "${RUN_PUBLISH_AFTER_GENERATE:-1}" != "0" ]]; then
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
