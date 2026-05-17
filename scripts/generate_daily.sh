#!/usr/bin/env bash
# Cron-runnable daily brief generation.
#
# Usage (local cron example):
#   0 1 * * 1-5 /Users/leonardodon/cn-altdata-brief/scripts/generate_daily.sh >> /tmp/cn-altdata-brief.log 2>&1
#
# Exit codes:
#   0 — brief written successfully
#   2 — all adapters failed; investigate upstream caches
#  >0 — uv / environment failure
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_ROOT"

if ! command -v uv >/dev/null 2>&1; then
    echo "[generate_daily] uv not found in PATH"
    exit 99
fi

uv sync --quiet
uv run cn-altdata-brief generate --verbose
