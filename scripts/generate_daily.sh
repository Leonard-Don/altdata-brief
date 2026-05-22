#!/usr/bin/env bash
# Cron-runnable daily brief generation.
#
# Usage (local cron example):
#   0 1 * * 1-5 /Users/leonardodon/altdata-brief/scripts/generate_daily.sh >> /tmp/altdata-brief.log 2>&1
#
# Exit codes:
#   0 — brief written successfully
#   2 — validate or generate failed; investigate upstream caches
#  >0 — uv / environment failure
#
# v0.2 adds a pre-flight `validate` pass. The brief refuses to publish
# when hard data-quality preconditions trip — empty industries, NaN
# metals, or incomplete CMA verdicts. WARN-level freshness signals (for
# example a stale ETF snapshot) are logged but do not stop local generation.
#
# v0.12 adds an opt-in content-quality pass. Set ALTDATA_BRIEF_STRICT=1
# in the environment to add --strict --fail-on-warn to the pre-flight
# validate call: fingerprint, density, consistency, and schema checks run;
# any WARN/FAIL or validator runtime error aborts before publishing.
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_ROOT"

if ! command -v uv >/dev/null 2>&1; then
    echo "[generate_daily] uv not found in PATH"
    exit 99
fi

uv sync --quiet

# Pre-flight: refuse to generate when data-quality preconditions FAIL.
# WARN-level signals (e.g. stale ETF snapshot) are tolerated locally;
# CI uses `--fail-on-warn` to be stricter. ALTDATA_BRIEF_STRICT=1
# additionally runs the v0.12 content-quality checks before generation.
validate_args=()
if [[ "${ALTDATA_BRIEF_STRICT:-0}" == "1" ]]; then
    validate_args+=(--strict --fail-on-warn)
    echo "[generate_daily] pre-flight validate --strict --fail-on-warn ..."
else
    echo "[generate_daily] pre-flight validate ..."
fi
set +e
uv run altdata-brief validate "${validate_args[@]}"
rc=$?
set -e
if [[ "${ALTDATA_BRIEF_STRICT:-0}" == "1" && "$rc" -ne 0 ]]; then
    echo "[generate_daily] strict validate FAILED/WARNED (exit=$rc); aborting before publish."
    exit "$rc"
fi
if [ "$rc" -ge 2 ]; then
    echo "[generate_daily] validate FAILED (exit=$rc); aborting before publish."
    exit "$rc"
fi
if [ "$rc" -gt 0 ]; then
    echo "[generate_daily] validate emitted warnings (exit=$rc); continuing."
fi

uv run altdata-brief generate --verbose
