"""Render tests: markdown valid, all sections present, chart files exist."""

from __future__ import annotations

from pathlib import Path

import pytest

from cn_altdata_brief.adapters import (
    ETF512400Adapter,
    IndexResearchAdapter,
    QuantTradingAdapter,
    SuperPricingAdapter,
)
from cn_altdata_brief.render import render_all_charts, render_brief_markdown, render_site_index
from cn_altdata_brief.synthesis import (
    synthesize_etf_flow,
    synthesize_industry,
    synthesize_inventory,
    synthesize_observation,
    synthesize_policy,
)


def _build_context(
    super_pricing_cache: Path,
    quant_trading_cache: Path,
    index_research_tables: Path,
    etf_512400_snapshot: Path,
    charts: dict[str, str] | None = None,
) -> dict:
    sp = SuperPricingAdapter(cache_dir=super_pricing_cache).fetch()
    qt = QuantTradingAdapter(cache_dir=quant_trading_cache).fetch()
    ix = IndexResearchAdapter(
        table_dir=index_research_tables, figure_dir=index_research_tables
    ).fetch()
    etf = ETF512400Adapter(snapshot_path=etf_512400_snapshot).fetch()
    return {
        "date": "2026-05-17",
        "fetched_at": "2026-05-17T00:00:00Z",
        "policy": synthesize_policy(sp),
        "inventory": synthesize_inventory(sp),
        "etf_flow": synthesize_etf_flow(etf, qt),
        "industry": synthesize_industry(qt),
        "observation": synthesize_observation(sp, qt, ix, etf),
        "charts": charts or {},
    }


def test_render_full_brief_contains_all_five_sections(
    super_pricing_cache: Path,
    quant_trading_cache: Path,
    index_research_tables: Path,
    etf_512400_snapshot: Path,
) -> None:
    ctx = _build_context(
        super_pricing_cache, quant_trading_cache, index_research_tables, etf_512400_snapshot
    )
    md = render_brief_markdown(context=ctx)
    assert "# CN AltData Brief — 2026-05-17" in md
    assert "## 1. 政策动向" in md
    assert "## 2. 库存信号" in md
    assert "## 3. ETF 资金流" in md
    assert "## 4. 行业温度" in md
    assert "## 5. 本日观察" in md
    assert "**Sources:**" in md
    # Disclaimer present
    assert "Disclaimer" in md


def test_render_brief_with_charts_embeds_image_tags(
    super_pricing_cache: Path,
    quant_trading_cache: Path,
    index_research_tables: Path,
    etf_512400_snapshot: Path,
) -> None:
    charts = {"policy": "../charts/x/policy_impact.png", "inventory": "../charts/x/inventory_change.png"}
    ctx = _build_context(
        super_pricing_cache, quant_trading_cache, index_research_tables, etf_512400_snapshot, charts
    )
    md = render_brief_markdown(context=ctx)
    assert "../charts/x/policy_impact.png" in md
    assert "../charts/x/inventory_change.png" in md


def test_render_all_charts_writes_png_files(tmp_path: Path) -> None:
    out = render_all_charts(
        output_dir=tmp_path,
        policy_top=[
            {"industry": "新能源汽车", "avg_impact": -0.38, "mentions": 94, "signal": "bearish"},
            {"industry": "电网", "avg_impact": 0.1, "mentions": 8, "signal": "neutral"},
        ],
        metals=[
            {"name_cn": "铜", "metal": "copper", "price_change_pct": -1.85},
            {"name_cn": "铝", "metal": "aluminium", "price_change_pct": 1.45},
        ],
        industry_top=[
            {"industry": "新能源汽车", "heat_score": 0.81},
            {"industry": "有色金属", "heat_score": 0.42},
        ],
        nav_trend=[
            {"date": "2026-04-29", "unit": 2.1},
            {"date": "2026-04-30", "unit": 2.12},
            {"date": "2026-05-04", "unit": 2.15},
        ],
    )
    assert set(out.keys()) == {"policy", "inventory", "industry", "nav"}
    for path in out.values():
        assert path.exists()
        assert path.stat().st_size > 0


def test_render_all_charts_skips_when_no_data(tmp_path: Path) -> None:
    out = render_all_charts(
        output_dir=tmp_path,
        policy_top=None,
        metals=None,
        industry_top=None,
        nav_trend=None,
    )
    assert out == {}


def test_render_site_index_empty_folder(tmp_path: Path) -> None:
    path = render_site_index(tmp_path)
    text = path.read_text(encoding="utf-8")
    assert "暂无简报" in text


def test_render_site_index_with_briefs(tmp_path: Path) -> None:
    (tmp_path / "2026-05-17.md").write_text("# brief 1", encoding="utf-8")
    (tmp_path / "2026-05-16.md").write_text("# brief 2", encoding="utf-8")
    path = render_site_index(tmp_path)
    text = path.read_text(encoding="utf-8")
    # newest first
    idx_17 = text.index("2026-05-17.md")
    idx_16 = text.index("2026-05-16.md")
    assert idx_17 < idx_16


def test_render_strict_undefined_catches_missing_keys(tmp_path: Path) -> None:
    from jinja2.exceptions import UndefinedError

    from cn_altdata_brief.render.markdown import render_brief_markdown

    # missing 'observation' should blow up loudly (strict undefined)
    with pytest.raises(UndefinedError):
        render_brief_markdown(
            context={
                "date": "2026-05-17",
                "fetched_at": "x",
                "policy": {"title": "x", "bullets": [], "sources": [], "policy_count": 0, "confidence": None, "timestamp": None, "top_industries": []},
                "inventory": {"title": "x", "bullets": [], "metals": [], "ports": None, "port_line": None, "sources": [], "timestamp": None},
                "etf_flow": {"title": "x", "bullets": [], "sources": [], "quote": {}, "adjacent": []},
                "industry": {"title": "x", "bullets": [], "sources": [], "top_industries": []},
                # observation missing on purpose
                "charts": {},
            }
        )
