"""Adapter fixture-based tests for each source project."""

from __future__ import annotations

from pathlib import Path

import pytest

from altdata_brief.adapters import (
    ETF512400Adapter,
    IndexResearchAdapter,
    QuantTradingAdapter,
    SuperPricingAdapter,
)
from altdata_brief.adapters.base import AdapterUnavailable


class TestSuperPricingAdapter:
    def test_fetch_cached_returns_normalized_policy(self, super_pricing_cache: Path) -> None:
        adapter = SuperPricingAdapter(cache_dir=super_pricing_cache)
        payload = adapter.fetch()
        assert payload.live is False
        assert payload.source == "super-pricing-system"
        policy = payload.data["policy_radar"]
        assert policy["policy_count"] == 12
        industries = policy["industry_signals"]
        assert len(industries) == 4
        # ordered by |avg_impact|, mentions
        assert industries[0]["industry"] == "新能源汽车"
        assert industries[0]["signal"] == "bearish"

    def test_fetch_cached_returns_normalized_macro(self, super_pricing_cache: Path) -> None:
        adapter = SuperPricingAdapter(cache_dir=super_pricing_cache)
        payload = adapter.fetch()
        macro = payload.data["macro_hf"]
        assert len(macro["metals"]) == 3
        names = [m["name_cn"] for m in macro["metals"]]
        assert "铜" in names
        assert macro["ports"]["global_index"] == 50.0

    def test_missing_cache_raises_unavailable(self, tmp_path: Path) -> None:
        empty = tmp_path / "empty"
        empty.mkdir()
        adapter = SuperPricingAdapter(cache_dir=empty)
        with pytest.raises(AdapterUnavailable):
            adapter.fetch()

    def test_cache_label_uses_filename(self, super_pricing_cache: Path) -> None:
        adapter = SuperPricingAdapter(cache_dir=super_pricing_cache)
        payload = adapter.fetch()
        assert "policy_radar.json" in payload.cache_label


class TestQuantTradingAdapter:
    def test_fetch_cached_derives_heat_from_policy(self, quant_trading_cache: Path) -> None:
        adapter = QuantTradingAdapter(cache_dir=quant_trading_cache)
        payload = adapter.fetch()
        rows = payload.data["industries"]
        assert len(rows) == 3
        # 新能源汽车 dominates mentions, should be rank 1
        assert rows[0]["industry"] == "新能源汽车"
        assert rows[0]["heat_score"] >= rows[1]["heat_score"] >= rows[2]["heat_score"]

    def test_missing_cache_raises(self, tmp_path: Path) -> None:
        adapter = QuantTradingAdapter(cache_dir=tmp_path)
        with pytest.raises(AdapterUnavailable):
            adapter.fetch()


class TestIndexResearchAdapter:
    def test_reads_verdicts_csv(self, index_research_tables: Path) -> None:
        adapter = IndexResearchAdapter(
            table_dir=index_research_tables, figure_dir=index_research_tables
        )
        payload = adapter.fetch()
        verdicts = payload.data["verdicts"]
        assert len(verdicts) == 7
        h2 = next(v for v in verdicts if v["hid"] == "H2")
        assert h2["verdict"] == "部分支持"
        assert h2["key_label"] == "US AUM ratio"

    def test_pap_changes_filtered(self, index_research_tables: Path) -> None:
        adapter = IndexResearchAdapter(
            table_dir=index_research_tables, figure_dir=index_research_tables
        )
        payload = adapter.fetch()
        changes = payload.data["pap_changes"]
        assert len(changes) == 1
        assert changes[0]["hid"] == "H2"

    def test_handles_missing_paths(self, tmp_path: Path) -> None:
        adapter = IndexResearchAdapter(table_dir=tmp_path, figure_dir=tmp_path)
        with pytest.raises(AdapterUnavailable):
            adapter.fetch()


class TestETF512400Adapter:
    def test_normalizes_snapshot(self, etf_512400_snapshot: Path) -> None:
        adapter = ETF512400Adapter(snapshot_path=etf_512400_snapshot)
        payload = adapter.fetch()
        data = payload.data
        assert data["code"] == "512400"
        assert data["source_health"]["verdict"] == "良好"
        assert data["source_health"]["required_ok"] == data["source_health"]["required_total"]
        assert data["commodity_drivers"]["ok_count"] == 5
        # navTrend tail of 5
        assert len(data["recent_nav"]) == 5

    def test_missing_snapshot(self, tmp_path: Path) -> None:
        adapter = ETF512400Adapter(snapshot_path=tmp_path / "nope.json")
        with pytest.raises(AdapterUnavailable):
            adapter.fetch()


class TestAdapterEnvFlag:
    def test_live_flag_defaults_to_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ALTDATA_BRIEF_LIVE", "1")
        adapter = SuperPricingAdapter()
        assert adapter.allow_live is True

        monkeypatch.setenv("ALTDATA_BRIEF_LIVE", "0")
        adapter = SuperPricingAdapter()
        assert adapter.allow_live is False

    def test_legacy_live_flag_defaults_to_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CN_ALTDATA_BRIEF_LIVE", "1")
        adapter = SuperPricingAdapter()
        assert adapter.allow_live is True

    def test_explicit_allow_live_overrides_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ALTDATA_BRIEF_LIVE", "1")
        adapter = SuperPricingAdapter(allow_live=False)
        assert adapter.allow_live is False
