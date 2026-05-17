#!/usr/bin/env bash
# Cron-runnable daily brief generation.
#
# Usage (local cron example):
#   0 1 * * 1-5 /Users/leonardodon/cn-altdata-brief/scripts/generate_daily.sh >> /tmp/cn-altdata-brief.log 2>&1
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
# CI uses `--fail-on-warn` to be stricter.
echo "[generate_daily] pre-flight validate ..."
set +e
uv run cn-altdata-brief validate
rc=$?
set -e
if [ "$rc" -ge 2 ]; then
    echo "[generate_daily] validate FAILED (exit=$rc); aborting before publish."
    exit "$rc"
fi
if [ "$rc" -gt 0 ]; then
    echo "[generate_daily] validate emitted warnings (exit=$rc); continuing."
fi

uv run cn-altdata-brief generate --verbose
