"""Render tests: markdown valid, all sections present, chart files exist."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from altdata_brief.adapters import (
    ETF512400Adapter,
    IndexResearchAdapter,
    QuantTradingAdapter,
    SuperPricingAdapter,
)
from altdata_brief.render import (
    render_all_charts,
    render_brief_markdown,
    render_feed,
    render_site_index,
)
from altdata_brief.synthesis import (
    synthesize_etf_flow,
    synthesize_industry,
    synthesize_inventory,
    synthesize_observation,
    synthesize_policy,
)
from altdata_brief.timefmt import format_beijing_time


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
    assert "# 多市场另类数据日报 — 2026-05-17" in md
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


def test_render_all_charts_tolerates_missing_row_keys(tmp_path: Path) -> None:
    """Chart rows missing industry / avg_impact must not raise KeyError —
    a malformed upstream row should render with placeholders, not abort.
    """
    out = render_all_charts(
        output_dir=tmp_path,
        policy_top=[{"mentions": 3}, {"industry": "电网", "avg_impact": 0.1}],
        metals=None,
        industry_top=[{"policy_signal": "neutral"}, {"industry": "有色金属", "heat_score": 0.4}],
        nav_trend=None,
    )
    assert out["policy"].exists()
    assert out["industry"].exists()


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
    assert "共 2 份日报，覆盖 2026-05-16 至 2026-05-17；最新在前。" in text
    # newest first
    idx_17 = text.index("2026-05-17.md")
    idx_16 = text.index("2026-05-16.md")
    assert idx_17 < idx_16
    assert "2026-05-17.en.md" not in text
    assert "latest.md" not in text
    assert "2026-99-99.md" not in text


def test_render_feed_with_briefs(tmp_path: Path) -> None:
    (tmp_path / "2026-05-17.md").write_text(
        "# AltData Brief — 2026-05-17\n\n- **新能源汽车**：daily summary\n",
        encoding="utf-8",
    )
    (tmp_path / "2026-05-16.md").write_text("# older", encoding="utf-8")
    feed = render_feed(
        briefs_dir=tmp_path,
        feed_path=tmp_path / "feed.xml",
        site_url="https://example.com/altdata-brief",
    )
    text = feed.read_text(encoding="utf-8")
    assert "<rss version=\"2.0\"" in text
    assert "https://example.com/altdata-brief/briefs/2026-05-17.html" in text
    assert "daily summary" in text


def test_render_strict_undefined_catches_missing_keys(tmp_path: Path) -> None:
    from jinja2.exceptions import UndefinedError

    from altdata_brief.render.markdown import render_brief_markdown

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
    assert format_beijing_time("2026-05-19T08:59:36Z") == "2026-05-19 16:59 北京时间"


def test_format_beijing_time_naive_datetime_treated_as_utc() -> None:
    naive = datetime(2026, 5, 19, 8, 59, 36)
    assert format_beijing_time(naive) == "2026-05-19 16:59 北京时间"


def test_format_beijing_time_already_beijing_no_double_shift() -> None:
    # ``+08:00`` value stays at its wall-clock minute — no extra +8.
    cn = datetime(2026, 5, 19, 16, 59, 36, tzinfo=timezone(timedelta(hours=8)))
    assert format_beijing_time(cn) == "2026-05-19 16:59 北京时间"


def test_format_beijing_time_with_seconds_includes_seconds() -> None:
    out = format_beijing_time("2026-05-19T08:59:36Z", with_seconds=True)
    assert out == "2026-05-19 16:59:36 北京时间"


def test_format_beijing_time_with_label_false_drops_suffix() -> None:
    out = format_beijing_time("2026-05-19T08:59:36Z", with_label=False)
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
    # The header line "由 altdata-brief 在 ..." is now Beijing time.
    assert "北京时间" in md
    # The frontmatter ``generated_at:`` line keeps the ISO-Z form so
    # RSS/Atom/OG consumers still see machine-readable timestamps.
    assert "generated_at: 2026-05-17T00:00:00Z" in md


# ---------------------------------------------------------------------------
# Industry rows hide noise for empty policy data (M2)
# ---------------------------------------------------------------------------


def test_industry_row_with_zero_impact_and_zero_mentions_renders_no_policy_marker() -> None:
    from altdata_brief.synthesis.industry import _format_row

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
    from altdata_brief.synthesis.industry import _format_row

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


# ---------------------------------------------------------------------------
# Setext H2 regression — ``**来源：**`` lines must not collapse into a
# heading by being placed immediately above a ``---`` underline. The
# brief template's policy + inventory partials end with an ``{% endif %}``
# that, under ``trim_blocks=True``, used to swallow the blank line and
# turn the next ``---`` into a CommonMark/GFM setext H2 underline.
# ---------------------------------------------------------------------------


def _minimal_brief_context() -> dict:
    """Smallest context the brief template accepts under StrictUndefined."""
    return {
        "date": "2026-05-17",
        "fetched_at": "2026-05-17T00:00:00Z",
        "policy": {
            "title": "政策动向",
            "bullets": ["**AI算力**: +1.0"],
            "sources": ["src::policy.json"],
            "policy_count": 1,
            "confidence": None,
            "timestamp": "2026-05-20T00:58:00Z",
            "top_industries": [],
        },
        "inventory": {
            "title": "库存信号",
            "bullets": ["x"],
            "sources": ["src::macro.json"],
            "metals": [],
            "ports": None,
            "port_line": None,
            "timestamp": "2026-05-19T23:59:00Z",
        },
        "etf_flow": {
            "title": "ETF 资金流",
            "bullets": ["x"],
            "sources": ["x"],
            "quote": {},
            "adjacent": [],
        },
        "industry": {
            "title": "行业温度",
            "bullets": ["x"],
            "sources": ["x"],
            "top_industries": [],
        },
        "observation": {
            "title": "本日观察",
            "sentences": ["今日核心信号"],
            "sources": ["x"],
        },
        "charts": {},
    }


def _has_blank_line_between(md: str, anchor: str, separator: str = "---") -> bool:
    """Return True when the line containing ``anchor`` is followed by a
    blank line before the next ``separator`` line.

    Setext H2 in CommonMark/GFM requires the underline (``---``) to be
    on the line immediately after a non-blank text line — inserting one
    blank line forces the parser to emit a paragraph + ``<hr>`` instead.
    """
    lines = md.splitlines()
    for i, line in enumerate(lines):
        if anchor in line:
            # Walk forward looking for the next non-empty content line.
            saw_blank = False
            for follower in lines[i + 1:]:
                if follower == "":
                    saw_blank = True
                    continue
                # First non-blank line after the anchor.
                return saw_blank and follower.strip() == separator
    return False


def test_policy_partial_keeps_blank_line_before_separator_so_no_setext_h2(
    super_pricing_cache: Path,
    quant_trading_cache: Path,
    index_research_tables: Path,
    etf_512400_snapshot: Path,
) -> None:
    """Regression: the policy partial's ``**来源：**`` line had ``---``
    on the very next line, which CommonMark/GFM renders as ``<h2>来源
    ：</h2>``. Verify there is at least one blank line between the
    source line and the section separator.
    """
    ctx = _build_context(
        super_pricing_cache,
        quant_trading_cache,
        index_research_tables,
        etf_512400_snapshot,
    )
    md = render_brief_markdown(context=ctx)
    # Find the policy section (## 1.) and check its 来源 line.
    section_start = md.find("## 1.")
    section_end = md.find("## 2.")
    assert section_start >= 0 and section_end > section_start
    policy_section = md[section_start:section_end]
    assert "**来源：**" in policy_section
    assert _has_blank_line_between(policy_section, "**来源：**", "---"), (
        "policy partial regressed: **来源：** must be followed by a blank "
        "line before the ``---`` separator, otherwise CommonMark/GFM "
        "renders it as a setext H2 underline."
    )


def test_inventory_partial_keeps_blank_line_before_separator_so_no_setext_h2(
    super_pricing_cache: Path,
    quant_trading_cache: Path,
    index_research_tables: Path,
    etf_512400_snapshot: Path,
) -> None:
    """Same regression as the policy partial, applied to inventory."""
    ctx = _build_context(
        super_pricing_cache,
        quant_trading_cache,
        index_research_tables,
        etf_512400_snapshot,
    )
    md = render_brief_markdown(context=ctx)
    section_start = md.find("## 2.")
    section_end = md.find("## 3.")
    assert section_start >= 0 and section_end > section_start
    inventory_section = md[section_start:section_end]
    assert "**来源：**" in inventory_section
    assert _has_blank_line_between(inventory_section, "**来源：**", "---"), (
        "inventory partial regressed: **来源：** must be followed by a "
        "blank line before the ``---`` separator (setext H2 trap)."
    )


def test_brief_template_does_not_emit_stacked_separators_before_metainfo(
    super_pricing_cache: Path,
    quant_trading_cache: Path,
    index_research_tables: Path,
    etf_512400_snapshot: Path,
) -> None:
    """Regression: brief.md.j2 used to add an explicit ``---`` after the
    observation partial — which itself ends with ``---``. Two consecutive
    ``---`` lines turn the first into ``<h2>---</h2>``. Verify only one
    separator precedes ``## 元信息``.
    """
    ctx = _build_context(
        super_pricing_cache,
        quant_trading_cache,
        index_research_tables,
        etf_512400_snapshot,
    )
    md = render_brief_markdown(context=ctx)
    metainfo_idx = md.find("## 元信息")
    assert metainfo_idx > 0
    # Walk backwards from ## 元信息 collecting separator-only lines.
    upstream = md[:metainfo_idx].rstrip("\n").splitlines()
    # Skip trailing blank lines.
    trailing = []
    for line in reversed(upstream):
        if line == "":
            continue
        trailing.append(line)
        if line.strip() != "---":
            break
    sep_count = sum(1 for line in trailing if line.strip() == "---")
    assert sep_count == 1, (
        f"brief.md.j2 regressed: expected exactly one ``---`` separator "
        f"directly above ## 元信息, found {sep_count} (would render as "
        f"<h2>---</h2>)."
    )


def test_brief_md_renders_source_line_as_paragraph_not_heading() -> None:
    """End-to-end markdown -> HTML check (Python ``markdown`` lib): the
    fixed brief must render every ``**来源：**`` line as ``<p>`` (with a
    ``<strong>`` child), never as ``<h2>``. Skipped when ``markdown`` is
    not installed (it isn't pinned in ``pyproject``).
    """
    import importlib

    try:
        md_lib = importlib.import_module("markdown")
    except ImportError:  # pragma: no cover — env-dependent
        import pytest

        pytest.skip("python-markdown not installed in this environment")

    from altdata_brief.render.markdown import render_brief_markdown

    md = render_brief_markdown(context=_minimal_brief_context())
    html = md_lib.markdown(md, extensions=["extra"])
    # Every 来源 line must be a paragraph, never a heading.
    assert "<h2><strong>来源" not in html
    assert "<h2>**来源" not in html
    # And we should still find the source line as a <p>.
    assert "<p><strong>来源" in html
    # No stacked-``---`` artifact above 元信息.
    assert "<h2>---</h2>" not in html
