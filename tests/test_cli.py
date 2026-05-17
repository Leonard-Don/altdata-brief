"""CLI smoke test — runs end-to-end against fixtures."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cn_altdata_brief import cli as cli_mod
from cn_altdata_brief import validate as validate_mod
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
from cn_altdata_brief.cli import main
from cn_altdata_brief.llm import RephraseResult


@pytest.fixture
def patched_default_paths(
    monkeypatch: pytest.MonkeyPatch,
    super_pricing_cache: Path,
    quant_trading_cache: Path,
    index_research_tables: Path,
    etf_512400_snapshot: Path,
) -> None:
    """Point all adapter defaults at fixture dirs (v0.4: 4 adapters)."""
    monkeypatch.setattr(sp_mod, "DEFAULT_CACHE_DIR", super_pricing_cache)
    monkeypatch.setattr(
        sp_mod,
        "DEFAULT_PUBLIC_SUMMARY",
        super_pricing_cache / "missing_public_summary.json",
    )
    monkeypatch.setattr(qt_mod, "DEFAULT_CACHE_DIR", quant_trading_cache)
    monkeypatch.setattr(
        qt_mod,
        "DEFAULT_PUBLIC_SUMMARY",
        quant_trading_cache / "missing_public_summary.json",
    )
    monkeypatch.setattr(ix_mod, "DEFAULT_TABLE_DIR", index_research_tables)
    monkeypatch.setattr(ix_mod, "DEFAULT_FIGURE_DIR", index_research_tables)
    monkeypatch.setattr(
        ix_mod,
        "DEFAULT_PUBLIC_SUMMARY",
        index_research_tables / "missing_public_summary.json",
    )
    monkeypatch.setattr(etf_mod, "DEFAULT_SNAPSHOT", etf_512400_snapshot)
    monkeypatch.setattr(etf_mod, "DEFAULT_PUBLIC_SUMMARY", etf_512400_snapshot)


def test_cli_generate_writes_brief(
    patched_default_paths: None,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    briefs = tmp_path / "briefs"
    charts = tmp_path / "charts"
    code = main(
        [
            "generate",
            "--date",
            "2026-05-17",
            "--briefs-dir",
            str(briefs),
            "--charts-dir",
            str(charts),
        ]
    )
    assert code == 0
    brief_path = briefs / "2026-05-17.md"
    assert brief_path.exists()
    text = brief_path.read_text(encoding="utf-8")
    assert "政策动向" in text
    assert "本日观察" in text
    # 4 charts produced
    chart_dir = charts / "2026-05-17"
    assert chart_dir.exists()
    pngs = list(chart_dir.glob("*.png"))
    assert len(pngs) >= 3
    # Captured summary line
    out = capsys.readouterr().out
    assert "OK" in out
    assert "5/5 sections available" in out
    assert (tmp_path / "feed.xml").exists()


def test_cli_generate_no_charts_flag(
    patched_default_paths: None, tmp_path: Path
) -> None:
    code = main(
        [
            "generate",
            "--date",
            "2026-05-17",
            "--briefs-dir",
            str(tmp_path / "b"),
            "--charts-dir",
            str(tmp_path / "c"),
            "--no-charts",
            "--no-index",
            "--no-feed",
        ]
    )
    assert code == 0
    # no chart subdirs created
    assert not (tmp_path / "c" / "2026-05-17").exists() or not any(
        (tmp_path / "c" / "2026-05-17").iterdir()
    )
    # no index.md when --no-index
    assert not (tmp_path / "b" / "index.md").exists()
    assert not (tmp_path / "feed.xml").exists()


def test_cli_generate_with_llm_uses_validated_polish(
    patched_default_paths: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    usage_log = tmp_path / "usage.jsonl"
    calls: list[tuple[str, dict, str]] = []
    logged: list[tuple[str, Path, dict]] = []

    def fake_rephrase(raw_text: str, context: dict, *, model: str) -> RephraseResult:
        calls.append((raw_text, context, model))
        return RephraseResult(
            raw_text=raw_text,
            polished_text=raw_text.replace("今日核心信号是", "今日最需要留意的是"),
            status="ok",
            llm_model_used=model,
            latency_ms=7.5,
            input_tokens=120,
            output_tokens=60,
        )

    def fake_log(result: RephraseResult, log_path: Path, *, extra: dict | None = None) -> None:
        logged.append((result.status, log_path, extra or {}))

    monkeypatch.setattr(cli_mod, "rephrase_observation", fake_rephrase)
    monkeypatch.setattr(cli_mod, "log_usage", fake_log)

    code = main(
        [
            "generate",
            "--date",
            "2026-05-17",
            "--briefs-dir",
            str(tmp_path / "briefs"),
            "--charts-dir",
            str(tmp_path / "charts"),
            "--no-charts",
            "--no-index",
            "--no-feed",
            "--with-llm",
            "--llm-model",
            "fake-model",
            "--llm-usage-log",
            str(usage_log),
        ]
    )

    assert code == 0
    text = (tmp_path / "briefs" / "2026-05-17.md").read_text(encoding="utf-8")
    assert "llm_requested: true" in text
    assert "llm_rephrase_used: true" in text
    assert "fake-model" in text
    assert "今日最需要留意的是" in text
    assert "原始规则化版本（deterministic source text）" in text
    assert calls and calls[0][1]["industries"]
    assert logged == [
        ("ok", usage_log, {"date": "2026-05-17", "section": "observation"})
    ]


def test_cli_generate_with_llm_fallback_renders_raw(
    patched_default_paths: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_rephrase(raw_text: str, context: dict, *, model: str) -> RephraseResult:
        return RephraseResult(
            raw_text=raw_text,
            polished_text=raw_text,
            status="validation_failed",
            llm_model_used=model,
            note="numbers missing",
        )

    monkeypatch.setattr(cli_mod, "rephrase_observation", fake_rephrase)
    monkeypatch.setattr(cli_mod, "log_usage", lambda *args, **kwargs: None)

    code = main(
        [
            "generate",
            "--date",
            "2026-05-17",
            "--briefs-dir",
            str(tmp_path / "briefs"),
            "--charts-dir",
            str(tmp_path / "charts"),
            "--no-charts",
            "--no-index",
            "--no-feed",
            "--with-llm",
        ]
    )

    assert code == 0
    text = (tmp_path / "briefs" / "2026-05-17.md").read_text(encoding="utf-8")
    assert "llm_requested: true" in text
    assert "llm_rephrase_used: false" in text
    assert "status=`validation_failed`" in text
    assert "原始规则化版本（deterministic source text）" not in text


def test_cli_validate_json_success(
    patched_default_paths: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(validate_mod, "MAX_ETF_SNAPSHOT_AGE_DAYS", 9999)
    code = main(["validate", "--json"])
    # WARN is acceptable here — the maintainer's local super-pricing repo
    # may or may not have a public summary fresh inside the 24h window.
    assert code in (0, 1)
    payload = json.loads(capsys.readouterr().out)
    assert payload["exit_code"] == code
    assert len(payload["checks"]) == 5
    names = [c["name"] for c in payload["checks"]]
    assert "public_summary_freshness" in names


def test_cli_help_works(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        main(["--help"])
    out = capsys.readouterr().out
    assert "cn-altdata-brief" in out
