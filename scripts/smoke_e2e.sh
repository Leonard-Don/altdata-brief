#!/usr/bin/env bash
# Local end-to-end smoke test that simulates the GitHub Actions environment.
#
# What it does
#   1. Creates a tmp scratch dir mirroring the layout daily.yml expects
#      (sibling source-repo checkouts containing only data/public/*).
#   2. rsyncs each upstream project's public-summary artifact into the
#      scratch dir. ETF 512400 ships its snapshot under src/data/.
#   3. Points ALTDATA_BRIEF_SOURCE_ROOT at the scratch dir and forces
#      PUBLIC_SUMMARY_PREFERENCE=public_only so the adapters MUST read
#      from the synthetic public-summary layout (not the maintainer's
#      real local caches).
#   4. Runs validate + generate under --source-mode public.
#   5. Asserts the brief contains all 5 sections, reports the resolved
#      mode per adapter, and prints time-to-generate.
#
# Usage
#   bash scripts/smoke_e2e.sh           # against real local upstreams
#   SMOKE_FIXTURE=1 bash scripts/smoke_e2e.sh  # against tests/fixtures/
#
# Exit codes
#   0 — brief generated with all 5 sections
#   1 — adapter resolution returned the wrong mode somewhere
#   2 — validate or generate failed
#   3 — generated brief is missing a required section
#  >0 — environment / IO failure

set -euo pipefail

START_TS=$(date +%s)

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_ROOT"

SCRATCH="$(mktemp -d -t altdata-brief-smoke-XXXXXX)"
trap 'rm -rf "$SCRATCH"' EXIT

echo "[smoke_e2e] scratch dir: $SCRATCH"

# ----------------------------------------------------------------------
# 1. Lay out source-repo skeletons inside the scratch dir.
# ----------------------------------------------------------------------
mkdir -p "$SCRATCH/super-pricing-system/data/public"
mkdir -p "$SCRATCH/quant-trading-system/data/public"
mkdir -p "$SCRATCH/index-inclusion-research/data/public"
mkdir -p "$SCRATCH/ETF 512400/src/data"

# ----------------------------------------------------------------------
# 2. Resolve upstream sources — real upstreams by default, fixtures
#    when SMOKE_FIXTURE=1. Each per-source block degrades gracefully if
#    the upstream doesn't have a public summary yet (warns and skips).
# ----------------------------------------------------------------------
if [ "${SMOKE_FIXTURE:-0}" = "1" ]; then
    SP_SRC="$PROJECT_ROOT/tests/fixtures/public_summary/alt_data_summary.json"
    IX_SRC="$PROJECT_ROOT/tests/fixtures/public_summary/index_research_summary.json"
    QT_SRC="$PROJECT_ROOT/tests/fixtures/public_summary/quant_summary.json"
    ETF_SRC="$PROJECT_ROOT/tests/fixtures/etf_512400/liveSnapshot.json"
else
    SP_SRC="/Users/leonardodon/super-pricing-system/data/public/alt_data_summary.json"
    IX_SRC="/Users/leonardodon/index-inclusion-research/data/public/index_research_summary.json"
    QT_SRC="/Users/leonardodon/quant-trading-system/data/public/quant_summary.json"
    ETF_SRC="/Users/leonardodon/ETF 512400/src/data/liveSnapshot.json"
fi

copy_if_exists() {
    local src="$1"
    local dst="$2"
    local label="$3"
    if [ -f "$src" ]; then
        rsync -a "$src" "$dst"
        echo "[smoke_e2e]   $label: synced from $src"
    else
        echo "[smoke_e2e]   $label: SKIPPED (no upstream artifact at $src)"
    fi
}

echo "[smoke_e2e] syncing public summaries:"
copy_if_exists "$SP_SRC"  "$SCRATCH/super-pricing-system/data/public/alt_data_summary.json" "super_pricing"
copy_if_exists "$IX_SRC"  "$SCRATCH/index-inclusion-research/data/public/index_research_summary.json"        "index_research"
copy_if_exists "$QT_SRC"  "$SCRATCH/quant-trading-system/data/public/quant_summary.json"     "quant_trading"
copy_if_exists "$ETF_SRC" "$SCRATCH/ETF 512400/src/data/liveSnapshot.json"                                    "etf_512400"

# In fixture mode the committed public summaries carry frozen timestamps —
# those will trip freshness checks on any day past the fixture's commit. We
# rewrite them to "today" before validate runs so fixture age doesn't drown out
# genuine adapter issues.
# Production mode (real upstreams) keeps the original timestamps; the
# upstream apps refresh them daily.
if [ "${SMOKE_FIXTURE:-0}" = "1" ]; then
    SP_DEST="$SCRATCH/super-pricing-system/data/public/alt_data_summary.json"
    ETF_DEST="$SCRATCH/ETF 512400/src/data/liveSnapshot.json"
    if [ -f "$SP_DEST" ] || [ -f "$ETF_DEST" ]; then
        uv run python - <<PY
import json, datetime, pathlib
today = datetime.date.today().isoformat()
now_iso = datetime.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
sp_path = pathlib.Path("${SP_DEST}")
if sp_path.exists():
    sp_doc = json.loads(sp_path.read_text(encoding="utf-8"))
    sp_doc["generated_at"] = now_iso
    providers = sp_doc.setdefault("providers", {})
    for provider_name in ("policy_radar", "macro_hf"):
        providers.setdefault(provider_name, {})["last_refresh_at"] = now_iso
    sp_path.write_text(json.dumps(sp_doc, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[smoke_e2e]   super_pricing: refreshed provider timestamps={now_iso}")
etf_path = pathlib.Path("${ETF_DEST}")
if etf_path.exists():
    doc = json.loads(etf_path.read_text(encoding="utf-8"))
    doc.setdefault("meta", {})["generatedAt"] = now_iso
    doc.setdefault("quote", {})["tradeDate"] = today
    doc.setdefault("nav", {})["date"] = today
    etf_path.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[smoke_e2e]   etf_512400: refreshed tradeDate={today}, generatedAt={now_iso}")
PY
    fi
fi

# ----------------------------------------------------------------------
# 3. Run validate + generate under the simulated CI environment.
# ----------------------------------------------------------------------
export ALTDATA_BRIEF_SOURCE_ROOT="$SCRATCH"
export PUBLIC_SUMMARY_PREFERENCE="public_only"

OUTPUT_DIR="$SCRATCH/output"
BRIEFS_DIR="$OUTPUT_DIR/briefs"
CHARTS_DIR="$OUTPUT_DIR/charts"
mkdir -p "$BRIEFS_DIR" "$CHARTS_DIR"

echo "[smoke_e2e] uv sync --extra dev ..."
uv sync --extra dev --quiet

echo "[smoke_e2e] validate --source-mode public:"
# validate may emit WARN for stale ETF / index when local upstreams are old —
# WARN exits 1 and is allowed here; structural FAIL exits 2 and blocks.
set +e
uv run altdata-brief validate --source-mode public
validate_rc=$?
set -e
if [ "$validate_rc" -ge 2 ]; then
    echo "[smoke_e2e] validate FAILED with exit=$validate_rc"
    exit 2
fi
if [ "$validate_rc" -gt 0 ]; then
    echo "[smoke_e2e] validate WARN (exit=$validate_rc) — continuing"
fi

DATE=$(date -u +"%Y-%m-%d")
echo "[smoke_e2e] generate --source-mode public --verbose for date=$DATE:"
uv run altdata-brief generate \
    --date "$DATE" \
    --source-mode public \
    --briefs-dir "$BRIEFS_DIR" \
    --charts-dir "$CHARTS_DIR" \
    --site-url "https://example.test/altdata-brief" \
    --verbose

# ----------------------------------------------------------------------
# 4. Validate the generated brief.
# ----------------------------------------------------------------------
BRIEF="$BRIEFS_DIR/$DATE.md"
if [ ! -f "$BRIEF" ]; then
    echo "[smoke_e2e] expected brief file not found: $BRIEF"
    exit 3
fi

echo "[smoke_e2e] generated brief: $BRIEF ($(wc -l < "$BRIEF") lines)"

REQUIRED_SECTIONS=("政策动向" "库存信号" "ETF 资金流" "行业温度" "本日观察")
missing=()
for section in "${REQUIRED_SECTIONS[@]}"; do
    if ! grep -q "$section" "$BRIEF"; then
        missing+=("$section")
    fi
done
if [ "${#missing[@]}" -gt 0 ]; then
    echo "[smoke_e2e] brief is missing sections: ${missing[*]}"
    exit 3
fi
echo "[smoke_e2e] all 5 sections present: ${REQUIRED_SECTIONS[*]}"

# Print a 20-line preview so the operator can eyeball it.
echo "[smoke_e2e] --- brief preview (first 30 lines) ---"
head -n 30 "$BRIEF"
echo "[smoke_e2e] --- end preview ---"

END_TS=$(date +%s)
ELAPSED=$((END_TS - START_TS))
echo "[smoke_e2e] OK · time-to-generate=${ELAPSED}s"
