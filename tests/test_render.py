"""Render tests: markdown valid, all sections present, chart files exist."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from cn_altdata_brief.adapters import (
    ETF512400Adapter,
    IndexResearchAdapter,
    QuantTradingAdapter,
    SuperPricingAdapter,
)
from cn_altdata_brief.render import (
    _format_beijing_time,
    render_all_charts,
    render_brief_markdown,
    render_feed,
    render_site_index,
)
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
    assert "# 中国另类数据日报 — 2026-05-17" in md
    assert "## 1. 政策动向" in md
    assert "## 2. 库存信号" in md
    assert "## 3. ETF 资金流" in md
    assert "## 4. 行业温度" in md
    assert "## 5. 本日观察" in md
    assert "**来源：**" in md
    assert "声明" in md
    assert "llm_requested: false" in md
    assert "llm_rephrase_used: false" in md
    assert "默认不调用大模型" in md
    assert "4 个公开摘要/快照数据源" in md
    assert "6 个项目组合" not in md
    assert "4 个本地量化项目" not in md


def test_render_brief_with_llm_polished_observation_keeps_raw_audit(
    super_pricing_cache: Path,
    quant_trading_cache: Path,
    index_research_tables: Path,
    etf_512400_snapshot: Path,
) -> None:
    ctx = _build_context(
        super_pricing_cache, quant_trading_cache, index_research_tables, etf_512400_snapshot
    )
    raw = ctx["observation"]["raw_text"]
    ctx["observation"]["polished_text"] = raw.replace("今日核心信号是", "今日最需要留意的是")
    ctx["llm"] = {
        "requested": True,
        "used": True,
        "status": "ok",
        "model": "fake-model",
        "latency_ms": 12.3,
        "input_tokens": 100,
        "output_tokens": 80,
        "raw_hash": "abc123",
        "note": None,
    }

    md = render_brief_markdown(context=ctx)

    assert "llm_requested: true" in md
    assert "llm_rephrase_used: true" in md
    assert "fake-model" in md
    assert "原始规则化版本" in md
    assert raw.splitlines()[0] in md


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
    assert "4 个公开摘要/快照数据源" in text
    assert "6 个量化项目" not in text


def test_render_site_index_with_briefs(tmp_path: Path) -> None:
    (tmp_path / "2026-05-17.md").write_text("# brief 1", encoding="utf-8")
    (tmp_path / "2026-05-16.md").write_text("# brief 2", encoding="utf-8")
    (tmp_path / "2026-05-17.en.md").write_text("# translated sibling", encoding="utf-8")
    (tmp_path / "latest.md").write_text("# latest alias", encoding="utf-8")
    (tmp_path / "2026-99-99.md").write_text("# malformed date", encoding="utf-8")
    path = render_site_index(tmp_path)
    text = path.read_text(encoding="utf-8")
    # newest first
    idx_17 = text.index("2026-05-17.md")
    idx_16 = text.index("2026-05-16.md")
    assert idx_17 < idx_16
    assert "2026-05-17.en.md" not in text
    assert "latest.md" not in text
    assert "2026-99-99.md" not in text


def test_render_feed_with_briefs(tmp_path: Path) -> None:
    (tmp_path / "2026-05-17.md").write_text(
        "# CN AltData Brief — 2026-05-17\n\n- **新能源汽车**：daily summary\n",
        encoding="utf-8",
    )
    (tmp_path / "2026-05-16.md").write_text("# older", encoding="utf-8")
    feed = render_feed(
        briefs_dir=tmp_path,
        feed_path=tmp_path / "feed.xml",
        site_url="https://example.com/cn-altdata-brief",
    )
    text = feed.read_text(encoding="utf-8")
    assert "<rss version=\"2.0\"" in text
    assert "https://example.com/cn-altdata-brief/briefs/2026-05-17.html" in text
    assert "daily summary" in text


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


# ---------------------------------------------------------------------------
# Beijing-time formatter (H2)
# ---------------------------------------------------------------------------


def test_format_beijing_time_iso_z_string() -> None:
    # UTC 08:59 → Beijing 16:59, default minute precision + suffix.
    assert _format_beijing_time("2026-05-19T08:59:36Z") == "2026-05-19 16:59 北京时间"


def test_format_beijing_time_naive_datetime_treated_as_utc() -> None:
    naive = datetime(2026, 5, 19, 8, 59, 36)
    assert _format_beijing_time(naive) == "2026-05-19 16:59 北京时间"


def test_format_beijing_time_already_beijing_no_double_shift() -> None:
    # ``+08:00`` value stays at its wall-clock minute — no extra +8.
    cn = datetime(2026, 5, 19, 16, 59, 36, tzinfo=timezone(timedelta(hours=8)))
    assert _format_beijing_time(cn) == "2026-05-19 16:59 北京时间"


def test_format_beijing_time_with_seconds_includes_seconds() -> None:
    out = _format_beijing_time("2026-05-19T08:59:36Z", with_seconds=True)
    assert out == "2026-05-19 16:59:36 北京时间"


def test_format_beijing_time_with_label_false_drops_suffix() -> None:
    out = _format_beijing_time("2026-05-19T08:59:36Z", with_label=False)
    assert out == "2026-05-19 16:59"
    assert "北京时间" not in out


# ---------------------------------------------------------------------------
# Brief body renders Beijing-time timestamps but frontmatter stays ISO Z
# ---------------------------------------------------------------------------


def test_render_brief_body_uses_beijing_time_for_reader_facing_stamps(
    super_pricing_cache: Path,
    quant_trading_cache: Path,
    index_research_tables: Path,
    etf_512400_snapshot: Path,
) -> None:
    ctx = _build_context(
        super_pricing_cache, quant_trading_cache, index_research_tables, etf_512400_snapshot
    )
    md = render_brief_markdown(context=ctx)
    # The header line "由 cn-altdata-brief 在 ..." is now Beijing time.
    assert "北京时间" in md
    # The frontmatter ``generated_at:`` line keeps the ISO-Z form so
    # RSS/Atom/OG consumers still see machine-readable timestamps.
    assert "generated_at: 2026-05-17T00:00:00Z" in md


# ---------------------------------------------------------------------------
# Industry rows hide noise for empty policy data (M2)
# ---------------------------------------------------------------------------


def test_industry_row_with_zero_impact_and_zero_mentions_renders_no_policy_marker() -> None:
    from cn_altdata_brief.synthesis.industry import _format_row

    out = _format_row(
        {
            "industry": "煤炭开采加工",
            "heat_score": 95.0,
            "policy_signal": "neutral",
            "policy_impact": 0.0,
            "mentions": 0,
        }
    )
    assert "无政策叠加" in out
    assert "中性（影响=+0.000）" not in out
    assert "提及次数=0" not in out


def test_industry_row_with_real_policy_data_keeps_existing_format() -> None:
    from cn_altdata_brief.synthesis.industry import _format_row

    out = _format_row(
        {
            "industry": "新能源汽车",
            "heat_score": 0.81,
            "policy_signal": "bullish",
            "policy_impact": 0.05,
            "mentions": 3,
        }
    )
    assert "政策叠加=利好（影响=+0.050）· 提及次数=3" in out
    assert "无政策叠加" not in out
