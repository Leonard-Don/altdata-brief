"""CLI smoke test — runs end-to-end against fixtures."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

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


@pytest.fixture
def patched_default_paths(
    monkeypatch: pytest.MonkeyPatch,
    super_pricing_cache: Path,
    quant_trading_cache: Path,
    index_research_tables: Path,
    etf_512400_snapshot: Path,
) -> None:
    """Point all adapter defaults at fixture dirs."""
    monkeypatch.setattr(sp_mod, "DEFAULT_CACHE_DIR", super_pricing_cache)
    monkeypatch.setattr(qt_mod, "DEFAULT_CACHE_DIR", quant_trading_cache)
    monkeypatch.setattr(ix_mod, "DEFAULT_TABLE_DIR", index_research_tables)
    monkeypatch.setattr(ix_mod, "DEFAULT_FIGURE_DIR", index_research_tables)
    monkeypatch.setattr(etf_mod, "DEFAULT_SNAPSHOT", etf_512400_snapshot)


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


def test_cli_validate_json_success(
    patched_default_paths: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(validate_mod, "MAX_ETF_SNAPSHOT_AGE_DAYS", 9999)
    code = main(["validate", "--json"])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["exit_code"] == 0
    assert len(payload["checks"]) == 4


def test_cli_help_works(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        main(["--help"])
    out = capsys.readouterr().out
    assert "cn-altdata-brief" in out
