"""Synthesis tests: empty input → degradation, happy path → expected content."""

from __future__ import annotations

from pathlib import Path

from cn_altdata_brief.adapters import (
    ETF512400Adapter,
    IndexResearchAdapter,
    QuantTradingAdapter,
    SuperPricingAdapter,
)
from cn_altdata_brief.adapters.base import AdapterPayload
from cn_altdata_brief.synthesis import (
    synthesize_etf_flow,
    synthesize_industry,
    synthesize_inventory,
    synthesize_observation,
    synthesize_policy,
)

# ---- policy ---------------------------------------------------------------


def test_policy_empty_input_degrades_gracefully() -> None:
    result = synthesize_policy(None)
    assert result["available"] is False
    assert "数据缺失" in result["bullets"][0]
    assert result["top_industries"] == []


def test_policy_happy_path_lists_top_3(super_pricing_cache: Path) -> None:
    payload = SuperPricingAdapter(cache_dir=super_pricing_cache).fetch()
    result = synthesize_policy(payload)
    assert result["available"] is True
    assert len(result["bullets"]) == 3
    assert "新能源汽车" in result["bullets"][0]
    assert result["policy_count"] == 12


# ---- inventory ------------------------------------------------------------


def test_inventory_empty_input_degrades() -> None:
    result = synthesize_inventory(None)
    assert result["available"] is False
    assert result["metals"] == []


def test_inventory_happy_path_tags_metals(super_pricing_cache: Path) -> None:
    payload = SuperPricingAdapter(cache_dir=super_pricing_cache).fetch()
    result = synthesize_inventory(payload)
    assert result["available"] is True
    assert len(result["metals"]) == 3
    text = " ".join(result["bullets"])
    assert "去库" in text  # 铜 falling triggers tag
    assert "累库" in text  # 铝 rising triggers tag
    assert "持稳" in text  # 镍 stable triggers tag


def test_inventory_zero_value_metal_collapses_to_no_weekly_change() -> None:
    """When both price_change_pct and volatility are 0, the format strips
    the noise prefix '周价格变化 +0.00% · 波动率 0.0' and renders '无周变动'
    instead — same pattern as the industry-section M2 fix (commit 1e92acd).
    """
    from cn_altdata_brief.synthesis.inventory import _format_metal

    line = _format_metal(
        {
            "metal": "Cu",
            "name_cn": "铜",
            "price_change_pct": 0.0,
            "volatility": 0.0,
            "trend": "rising",
            "confidence": 0.01,
        }
    )
    assert "无周变动" in line
    assert "+0.00%" not in line
    assert "波动率 0.0" not in line
    # Trend, label, confidence still rendered
    assert "趋势=上行" in line
    assert "置信度=0.01" in line


def test_inventory_nonzero_value_metal_preserves_existing_format() -> None:
    """Non-zero data path is unchanged — full format with percent + volatility."""
    from cn_altdata_brief.synthesis.inventory import _format_metal

    line = _format_metal(
        {
            "metal": "Cu",
            "name_cn": "铜",
            "price_change_pct": -1.85,
            "volatility": 0.42,
            "trend": "falling",
            "confidence": 0.75,
        }
    )
    assert "周价格变化 -1.85%" in line
    assert "波动率 0.4" in line
    assert "无周变动" not in line


# ---- etf_flow -------------------------------------------------------------


def test_etf_flow_empty_etf_payload() -> None:
    result = synthesize_etf_flow(None, None)
    assert result["available"] is False
    assert "数据缺失" in result["bullets"][0]


def test_etf_flow_uses_both_payloads(
    etf_512400_snapshot: Path, quant_trading_cache: Path
) -> None:
    etf_payload = ETF512400Adapter(snapshot_path=etf_512400_snapshot).fetch()
    quant_payload = QuantTradingAdapter(cache_dir=quant_trading_cache).fetch()
    result = synthesize_etf_flow(etf_payload, quant_payload)
    assert result["available"] is True
    assert "有色金属ETF南方" in result["bullets"][0]
    # adjacent industry detection should find 有色金属 from quant cache
    text = " ".join(result["bullets"])
    assert "有色金属" in text


def test_etf_flow_handles_missing_quant(etf_512400_snapshot: Path) -> None:
    etf_payload = ETF512400Adapter(snapshot_path=etf_512400_snapshot).fetch()
    result = synthesize_etf_flow(etf_payload, None)
    assert result["available"] is True
    assert result["adjacent"] == []


def test_etf_flow_hides_degraded_quote_price() -> None:
    payload = AdapterPayload(
        source="ETF-512400",
        fetched_at="2026-05-19T00:00:00Z",
        cache_path=None,
        live=False,
        data={
            "name": "有色金属ETF南方",
            "code": "512400",
            "trade_date": "2026-05-07",
            "generated_at": "2026-05-19T08:54:01Z",
            "price": 2.207,
            "change_percent": 0.0036,
            "nav": {"date": "2026-05-18", "unit": 2.0081, "daily_return": -0.0155},
            "source_health": {
                "required_total": 4,
                "required_ok": 3,
                "fallback": 1,
                "verdict": "降级",
                "quote_ok": False,
                "quote_fallback": True,
            },
            "commodity_drivers": {},
        },
    )
    result = synthesize_etf_flow(payload, None)
    assert "行情源降级" in result["bullets"][0]
    assert "现价 2.207" not in " ".join(result["bullets"])


def test_etf_flow_tolerates_string_typed_price_and_unit() -> None:
    """Upstream JS snapshots sometimes stringify numbers; quote/nav
    formatting must coerce rather than crash on f-string numeric specs.
    """
    payload = AdapterPayload(
        source="ETF-512400",
        fetched_at="2026-05-19T00:00:00Z",
        cache_path=None,
        live=False,
        data={
            "name": "有色金属ETF南方",
            "code": "512400",
            "price": "2.207",
            "change_percent": 0.0036,
            "nav": {"date": "2026-05-18", "unit": "2.0081", "daily_return": -0.0155},
            "source_health": {
                "required_total": 4,
                "required_ok": 4,
                "fallback": 0,
                "verdict": "正常",
                "quote_ok": True,
                "quote_fallback": False,
            },
            "commodity_drivers": {},
        },
    )
    result = synthesize_etf_flow(payload, None)
    assert result["available"] is True
    text = " ".join(result["bullets"])
    assert "现价 2.207" in text
    assert "单位净值 2.0081" in text


# ---- industry -------------------------------------------------------------


def test_industry_empty_input() -> None:
    result = synthesize_industry(None)
    assert result["available"] is False


def test_industry_happy_path(quant_trading_cache: Path) -> None:
    payload = QuantTradingAdapter(cache_dir=quant_trading_cache).fetch()
    result = synthesize_industry(payload)
    assert result["available"] is True
    assert len(result["bullets"]) == 3
    assert "新能源汽车" in result["bullets"][0]


# ---- observation ----------------------------------------------------------


def test_observation_all_missing_returns_缺失() -> None:
    result = synthesize_observation(None, None, None, None)
    assert result["available"] is False
    assert "数据缺失" in result["sentences"][0]
    assert result["raw_text"] == result["sentences"][0]
    assert result["industries"] == []


def test_observation_with_super_and_etf_produces_sentences(
    super_pricing_cache: Path, etf_512400_snapshot: Path
) -> None:
    sp = SuperPricingAdapter(cache_dir=super_pricing_cache).fetch()
    etf = ETF512400Adapter(snapshot_path=etf_512400_snapshot).fetch()
    result = synthesize_observation(sp, None, None, etf)
    assert result["available"] is True
    # v0.2 always emits exactly 3 sentences (framing / context / action).
    assert len(result["sentences"]) == 3
    assert result["raw_text"] == "\n".join(result["sentences"])
    assert "有色金属" in result["industries"]
    assert "良好" not in result["industries"]
    framing, context, action = result["sentences"]
    assert framing.startswith("今日核心信号是")
    assert "近 7 日" in context
    assert action.startswith("若该信号延续")
    banned = ("抢权", "离场", "配对交易", "买入", "卖出", "加仓", "减仓", "投资建议")
    assert not any(term in " ".join(result["sentences"]) for term in banned)


def test_observation_picks_up_pap_changes(index_research_tables: Path) -> None:
    payload = IndexResearchAdapter(
        table_dir=index_research_tables, figure_dir=index_research_tables
    ).fetch()
    result = synthesize_observation(None, None, payload, None)
    assert result["available"] is True
    assert "迁移" in " ".join(result["sentences"])


def test_observation_no_pap_changes_reports_stability(tmp_path: Path) -> None:
    # build an index_research adapter with only verdicts (no PAP)
    verdicts = tmp_path / "cma_hypothesis_verdicts.csv"
    verdicts.write_text(
        "hid,name_cn,verdict,confidence,evidence_summary,metric_snapshot,next_step,evidence_refs,p_value,key_label,key_value,n_obs,paper_ids,paper_count,track,evidence_tier\n"
        "H1,信息泄露与预运行,支持,高,x,x,x,M1,0.05,bootstrap p,0.05,100,h_1986,1,identification,core\n"
        "H2,价格压力,证据不足,中,x,x,x,M2,0.3,p,0.3,100,p_2011,1,demand,core\n",
        encoding="utf-8",
    )
    payload = IndexResearchAdapter(table_dir=tmp_path, figure_dir=tmp_path).fetch()
    result = synthesize_observation(None, None, payload, None)
    assert result["available"] is True
    text = " ".join(result["sentences"])
    assert "保持稳定" in text
