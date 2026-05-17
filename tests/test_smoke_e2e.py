"""Python-level wrapper for the smoke_e2e.sh logic.

We do NOT shell out to scripts/smoke_e2e.sh — the harness has its own
guarantees (uv sync, the user's shell) that pytest shouldn't depend on.
Instead, this test exercises the SAME logic the shell script encodes:

* a synthetic scratch dir that mirrors ``CN_ALTDATA_BRIEF_SOURCE_ROOT``;
* fixture public-summary files rsynced into the per-source paths the
  adapters expect;
* ``PUBLIC_SUMMARY_PREFERENCE=public_only`` so the adapters MUST hit
  the synthetic public summaries (never the maintainer's real caches);
* validate + generate run under those env vars;
* the generated brief is asserted to carry all 5 section headers.

These two tests double as the "what the smoke script does" specification
— if the shell script ever drifts, this is the parity check.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from cn_altdata_brief.adapters import (
    ETF512400Adapter,
    IndexResearchAdapter,
    QuantTradingAdapter,
    SuperPricingAdapter,
    build_default_adapters,
)
from cn_altdata_brief.config import load_source_config

FIXTURES = Path(__file__).parent / "fixtures"
PUBLIC_FIXTURES = FIXTURES / "public_summary"
SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "smoke_e2e.sh"


# ---------------------------------------------------------------------------
# Sub-test 1 — Python-level parity of the smoke script
# ---------------------------------------------------------------------------


def _seed_source_root(scratch: Path) -> dict[str, Path]:
    """Lay out the same directory tree smoke_e2e.sh creates.

    Returns a dict of {source_key: destination_path} for assertion.
    """
    layouts = {
        "super_pricing": (
            scratch / "PycharmProjects" / "super-pricing-system" / "data" / "public",
            PUBLIC_FIXTURES / "alt_data_summary.json",
            "alt_data_summary.json",
        ),
        "index_research": (
            scratch / "index-inclusion-research" / "data" / "public",
            PUBLIC_FIXTURES / "index_research_summary.json",
            "index_research_summary.json",
        ),
        "quant_trading": (
            scratch / "PycharmProjects" / "quant-trading-system" / "data" / "public",
            PUBLIC_FIXTURES / "quant_summary.json",
            "quant_summary.json",
        ),
        "etf_512400": (
            scratch / "ETF 512400" / "src" / "data",
            FIXTURES / "etf_512400" / "liveSnapshot.json",
            "liveSnapshot.json",
        ),
    }
    destinations: dict[str, Path] = {}
    for source_key, (target_dir, src, leaf) in layouts.items():
        target_dir.mkdir(parents=True, exist_ok=True)
        dst = target_dir / leaf
        shutil.copyfile(src, dst)
        destinations[source_key] = dst

    # The committed ETF fixture has a frozen tradeDate that would trip
    # the snapshot-age check on any day past the fixture's commit. Same
    # workaround as scripts/smoke_e2e.sh: refresh the dates in the copy.
    import json
    from datetime import UTC, datetime
    etf_dst = destinations["etf_512400"]
    doc = json.loads(etf_dst.read_text(encoding="utf-8"))
    today = datetime.now(UTC).date().isoformat()
    now_iso = datetime.now(UTC).replace(microsecond=0).isoformat()
    doc.setdefault("meta", {})["generatedAt"] = now_iso
    doc.setdefault("quote", {})["tradeDate"] = today
    doc.setdefault("nav", {})["date"] = today
    etf_dst.write_text(
        json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return destinations


def _reload_config_with_root(monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
    """Point both the config module and the adapter default paths at ``root``."""
    monkeypatch.setenv("CN_ALTDATA_BRIEF_SOURCE_ROOT", str(root))
    # The config module's module-level constants are evaluated at import
    # time. Re-resolve them so the adapters' DEFAULT_PUBLIC_SUMMARY picks
    # up our scratch root.
    from cn_altdata_brief import config as cfg_mod
    from cn_altdata_brief.adapters import (
        etf_512400 as etf_mod,
    )
    from cn_altdata_brief.adapters import (
        index_research as ix_mod,
    )
    from cn_altdata_brief.adapters import (
        quant_trading as qt_mod,
    )
    from cn_altdata_brief.adapters import (
        super_pricing as sp_mod,
    )

    monkeypatch.setattr(cfg_mod, "DEFAULT_SOURCE_REPOS_ROOT", root)
    monkeypatch.setitem(
        cfg_mod.SOURCE_REPO_DIRS,
        "super_pricing",
        root / "PycharmProjects" / "super-pricing-system",
    )
    monkeypatch.setitem(
        cfg_mod.SOURCE_REPO_DIRS,
        "quant_trading",
        root / "PycharmProjects" / "quant-trading-system",
    )
    monkeypatch.setitem(
        cfg_mod.SOURCE_REPO_DIRS,
        "index_research",
        root / "index-inclusion-research",
    )
    monkeypatch.setitem(
        cfg_mod.SOURCE_REPO_DIRS,
        "etf_512400",
        root / "ETF 512400",
    )
    monkeypatch.setattr(
        sp_mod, "DEFAULT_PUBLIC_SUMMARY",
        cfg_mod.public_summary_path("super_pricing"),
    )
    monkeypatch.setattr(
        ix_mod, "DEFAULT_PUBLIC_SUMMARY",
        cfg_mod.public_summary_path("index_research"),
    )
    monkeypatch.setattr(
        qt_mod, "DEFAULT_PUBLIC_SUMMARY",
        cfg_mod.public_summary_path("quant_trading"),
    )
    monkeypatch.setattr(
        etf_mod, "DEFAULT_PUBLIC_SUMMARY",
        cfg_mod.public_summary_path("etf_512400"),
    )
    monkeypatch.setattr(
        etf_mod, "DEFAULT_SNAPSHOT",
        cfg_mod.public_summary_path("etf_512400"),
    )
    # Likewise wipe the cache defaults so a stray fixture cache doesn't
    # bleed in through the auto fallback.
    monkeypatch.setattr(sp_mod, "DEFAULT_CACHE_DIR", root / "no-cache-sp")
    monkeypatch.setattr(qt_mod, "DEFAULT_CACHE_DIR", root / "no-cache-qt")
    monkeypatch.setattr(ix_mod, "DEFAULT_TABLE_DIR", root / "no-cache-ix")
    monkeypatch.setattr(ix_mod, "DEFAULT_FIGURE_DIR", root / "no-cache-ix-fig")


def test_smoke_e2e_python_parity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same end-to-end shape as scripts/smoke_e2e.sh, but driven from Python."""
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    _seed_source_root(scratch)
    _reload_config_with_root(monkeypatch, scratch)
    monkeypatch.setenv("PUBLIC_SUMMARY_PREFERENCE", "public_only")

    from cn_altdata_brief.cli import main as cli_main

    briefs_dir = tmp_path / "briefs"
    charts_dir = tmp_path / "charts"

    start = time.perf_counter()
    rc_validate = cli_main(["validate", "--source-mode", "public", "--json"])
    rc_generate = cli_main(
        [
            "generate",
            "--date",
            "2026-05-17",
            "--source-mode",
            "public",
            "--briefs-dir",
            str(briefs_dir),
            "--charts-dir",
            str(charts_dir),
            "--no-feed",
        ]
    )
    elapsed = time.perf_counter() - start

    # validate may exit 0 (INFO) or 1 (WARN — stale fixture) but never 2 (FAIL).
    assert rc_validate in (0, 1), f"validate exit={rc_validate}"
    assert rc_generate == 0
    brief_path = briefs_dir / "2026-05-17.md"
    assert brief_path.exists()
    text = brief_path.read_text(encoding="utf-8")
    for section in ("政策动向", "库存信号", "ETF 资金流", "行业温度", "本日观察"):
        assert section in text, f"brief missing section {section}"
    # Sanity: must complete fast.
    assert elapsed < 30, f"smoke e2e took {elapsed:.2f}s (>30s budget)"


def test_public_summary_generate_keeps_llm_disabled_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Public-summary fixture generation must not require or invoke LLMs."""
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    _seed_source_root(scratch)
    _reload_config_with_root(monkeypatch, scratch)
    monkeypatch.setenv("PUBLIC_SUMMARY_PREFERENCE", "public_only")

    from cn_altdata_brief import cli as cli_mod
    from cn_altdata_brief.cli import main as cli_main

    def fail_if_called(*args: object, **kwargs: object) -> object:
        raise AssertionError("LLM rephrase should not be called without --with-llm")

    monkeypatch.setattr(cli_mod, "rephrase_observation", fail_if_called)

    briefs_dir = tmp_path / "briefs"
    rc_generate = cli_main(
        [
            "generate",
            "--date",
            "2026-05-17",
            "--source-mode",
            "public",
            "--briefs-dir",
            str(briefs_dir),
            "--charts-dir",
            str(tmp_path / "charts"),
            "--no-charts",
            "--no-index",
            "--no-feed",
        ]
    )

    assert rc_generate == 0
    text = (briefs_dir / "2026-05-17.md").read_text(encoding="utf-8")
    assert "llm_requested: false" in text
    assert "llm_rephrase_used: false" in text
    assert "llm_status: disabled" in text
    assert "llm_model: null" in text
    assert "默认不调用 LLM" in text
    assert "原始规则化版本（deterministic source text）" not in text
    assert str(scratch) not in text
    assert "/Users/leonardodon" not in text


def test_public_summary_generate_with_unavailable_llm_falls_back_to_raw(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Even explicit --with-llm cannot make public fixture generation depend on LLMs."""
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    _seed_source_root(scratch)
    _reload_config_with_root(monkeypatch, scratch)
    monkeypatch.setenv("PUBLIC_SUMMARY_PREFERENCE", "public_only")

    from cn_altdata_brief import cli as cli_mod
    from cn_altdata_brief.llm import anthropic_client as llm_client

    monkeypatch.setattr(llm_client, "_sdk_module", lambda: None)

    usage_log = tmp_path / "llm_usage.jsonl"
    briefs_dir = tmp_path / "briefs"
    rc_generate = cli_mod.main(
        [
            "generate",
            "--date",
            "2026-05-17",
            "--source-mode",
            "public",
            "--briefs-dir",
            str(briefs_dir),
            "--charts-dir",
            str(tmp_path / "charts"),
            "--no-charts",
            "--no-index",
            "--no-feed",
            "--with-llm",
            "--llm-usage-log",
            str(usage_log),
        ]
    )

    assert rc_generate == 0
    text = (briefs_dir / "2026-05-17.md").read_text(encoding="utf-8")
    assert "llm_requested: true" in text
    assert "llm_rephrase_used: false" in text
    assert "llm_status: sdk_missing" in text
    assert "LLM 改写已请求但未用于正文" in text
    assert "原始规则化版本（deterministic source text）" not in text
    assert str(scratch) not in text
    assert "/Users/leonardodon" not in text

    records = [
        json.loads(line)
        for line in usage_log.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(records) == 1
    record = records[0]
    assert record["model"] is None
    assert record["status"] == "sdk_missing"
    assert record["input_tokens"] is None
    assert record["output_tokens"] is None
    assert record["latency_ms"] is None
    assert record["est_cost_usd"] == 0.0
    assert record["prompt_hash"] == ""
    assert record["date"] == "2026-05-17"
    assert record["section"] == "observation"
    assert "raw_text" not in record
    assert "polished_text" not in record


# ---------------------------------------------------------------------------
# Sub-test 2 — All 4 adapters resolve to "public" in this scratch env
# ---------------------------------------------------------------------------


def test_all_four_adapters_resolve_public(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    _seed_source_root(scratch)
    _reload_config_with_root(monkeypatch, scratch)
    monkeypatch.setenv("PUBLIC_SUMMARY_PREFERENCE", "public_only")

    cfg = load_source_config(preference="public_only")
    adapters = build_default_adapters(config=cfg)

    resolutions = {name: adapter.resolve_source() for name, adapter in adapters.items()}
    for name, res in resolutions.items():
        assert res.available is True, (
            f"adapter {name} not available: mode={res.mode} note={res.note}"
        )
        assert res.mode == "public", (
            f"adapter {name} resolved to mode={res.mode}, expected public"
        )
        assert res.path is not None
        assert res.path.exists()


# ---------------------------------------------------------------------------
# Sub-test 3 — Adapter constructors don't crash under public_only
# ---------------------------------------------------------------------------


def test_each_adapter_under_public_only_uses_correct_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    paths = _seed_source_root(scratch)
    _reload_config_with_root(monkeypatch, scratch)

    cfg = load_source_config(preference="public_only")
    sp = SuperPricingAdapter(config=cfg).fetch()
    qt = QuantTradingAdapter(config=cfg).fetch()
    ix = IndexResearchAdapter(config=cfg).fetch()
    etf = ETF512400Adapter(config=cfg).fetch()

    assert sp.data["source_mode"] == "public"
    assert qt.data["source_mode"] == "public"
    assert ix.data["source_mode"] == "public"
    assert etf.data["source_mode"] == "public"
    # Each adapter's cache_path is the public summary file we seeded.
    assert sp.cache_path == paths["super_pricing"]
    assert qt.cache_path == paths["quant_trading"]
    assert ix.cache_path == paths["index_research"]
    assert etf.cache_path == paths["etf_512400"]


# ---------------------------------------------------------------------------
# Sub-test 4 — smoke_e2e.sh script exists and is executable
# ---------------------------------------------------------------------------


def test_smoke_e2e_shell_script_is_executable() -> None:
    """Catch accidental chmod regressions on the shell script."""
    import os
    assert SCRIPT.exists(), f"missing {SCRIPT}"
    assert os.access(SCRIPT, os.X_OK), f"{SCRIPT} is not executable"


# ---------------------------------------------------------------------------
# Sub-test 5 — when SMOKE_FIXTURE=1, the shell script's fixture-mode
# resolution paths must align with what tests/fixtures actually contains.
# ---------------------------------------------------------------------------


def test_smoke_script_fixture_mode_paths_exist() -> None:
    """Guard against the SMOKE_FIXTURE=1 branch pointing at vapor files."""
    expected = [
        FIXTURES / "public_summary" / "alt_data_summary.json",
        FIXTURES / "public_summary" / "index_research_summary.json",
        FIXTURES / "public_summary" / "quant_summary.json",
        FIXTURES / "etf_512400" / "liveSnapshot.json",
    ]
    for path in expected:
        assert path.exists(), f"fixture referenced by smoke_e2e.sh missing: {path}"


def test_smoke_script_preserves_dev_env_and_warn_semantics() -> None:
    """The shell smoke must not turn stale local data WARNs into hard FAILs.

    It also runs ``uv sync`` before invoking the CLI; keep the dev extra so
    running the script during local closeout does not uninstall pytest/ruff
    from the shared project venv mid-session.
    """

    text = SCRIPT.read_text(encoding="utf-8")
    assert "uv sync --extra dev --quiet" in text
    assert "validate --source-mode public\n" in text
    assert "validate --source-mode public --fail-on-warn" not in text


# ---------------------------------------------------------------------------
# Sub-test 6 (slow, opt-in) — actually invoke scripts/smoke_e2e.sh
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    shutil.which("bash") is None,
    reason="bash not on PATH; the wrapper test above already exercises the same logic",
)
def test_smoke_e2e_shell_invocation_under_fixture_mode(tmp_path: Path) -> None:
    """Run the actual shell script under SMOKE_FIXTURE=1.

    This is the highest-fidelity test — invokes scripts/smoke_e2e.sh
    end-to-end. Marked as the slow one; sub-tests above cover the
    semantics faster. The 30s budget enforced here mirrors the v0.4
    constraint ("smoke test must complete in < 30 seconds locally").
    """
    project_root = Path(__file__).resolve().parents[1]
    env = {
        **__import__("os").environ,
        "SMOKE_FIXTURE": "1",
    }
    start = time.perf_counter()
    proc = subprocess.run(
        ["bash", str(SCRIPT)],
        cwd=project_root,
        env=env,
        capture_output=True,
        timeout=60,
        check=False,
    )
    elapsed = time.perf_counter() - start
    if proc.returncode != 0:
        print(proc.stdout.decode("utf-8", errors="replace"))
        print(proc.stderr.decode("utf-8", errors="replace"))
    assert proc.returncode == 0, f"smoke_e2e.sh exited {proc.returncode}"
    assert elapsed < 30, f"smoke_e2e.sh took {elapsed:.2f}s (>30s budget)"
    assert b"all 5 sections present" in proc.stdout
