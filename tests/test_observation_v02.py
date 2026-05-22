"""v0.2 polish: observation section follows framing → context → follow-up shape."""

from __future__ import annotations

from pathlib import Path

from altdata_brief.adapters import (
    ETF512400Adapter,
    IndexResearchAdapter,
    QuantTradingAdapter,
    SuperPricingAdapter,
)
from altdata_brief.synthesis import synthesize_observation
from altdata_brief.synthesis.baseline import (
    SIGNAL_PERSISTENCE_DAYS,
    describe_intensity,
)


def test_observation_always_3_sentences_when_signal_present(
    super_pricing_cache: Path,
) -> None:
    sp = SuperPricingAdapter(cache_dir=super_pricing_cache).fetch()
    result = synthesize_observation(sp, None, None, None)
    assert result["available"] is True
    assert len(result["sentences"]) == 3


def test_observation_three_sentence_pattern_holds_for_all_inputs(
    super_pricing_cache: Path,
    quant_trading_cache: Path,
    index_research_tables: Path,
    etf_512400_snapshot: Path,
) -> None:
    sp = SuperPricingAdapter(cache_dir=super_pricing_cache).fetch()
    qt = QuantTradingAdapter(cache_dir=quant_trading_cache).fetch()
    ix = IndexResearchAdapter(
        table_dir=index_research_tables, figure_dir=index_research_tables
    ).fetch()
    etf = ETF512400Adapter(snapshot_path=etf_512400_snapshot).fetch()
    result = synthesize_observation(sp, qt, ix, etf)
    framing, context, action = result["sentences"]
    assert framing.startswith("今日核心信号是")
    assert "近 7 日" in context or "近周" in context
    assert action.startswith("若该信号延续")
    assert f"{SIGNAL_PERSISTENCE_DAYS} 日" in action


def test_observation_picks_strongest_candidate(
    super_pricing_cache: Path, etf_512400_snapshot: Path
) -> None:
    # ETF NAV (3.85%) outweighs policy impact (~0.388) under v0.2 strength ranking
    # because the ETF strength is expressed in pp (3.85) and policy in raw impact (~0.4).
    sp = SuperPricingAdapter(cache_dir=super_pricing_cache).fetch()
    etf = ETF512400Adapter(snapshot_path=etf_512400_snapshot).fetch()
    result = synthesize_observation(sp, None, None, etf)
    joined = " ".join(result["sentences"])
    # ETF candidate wins → headline mentions 512400 or NAV direction.
    assert "ETF 512400" in joined or "NAV" in joined


def test_describe_intensity_returns_distinct_buckets() -> None:
    assert describe_intensity(0.4, 0.18) == "显著走强"
    assert describe_intensity(0.18, 0.18) == "与近期持平"
    assert describe_intensity(0.02, 0.18) == "明显衰减"
    assert describe_intensity(0.5, 0.0) == "无可比基线"
