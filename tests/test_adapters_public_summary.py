"""Tests for the public-summary resolution path on each adapter.

Covers:

* Public summary present + cache present → public used.
* Public summary absent + cache present → cache used (regression guard).
* Both absent → raises ``AdapterUnavailable``.
* ``PUBLIC_SUMMARY_PREFERENCE=public_only`` + missing → raises clearly.
* Schema mapping: public summary fields populate the same internal shape
  as the cache.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cn_altdata_brief.adapters import (
    IndexResearchAdapter,
    SuperPricingAdapter,
)
from cn_altdata_brief.adapters.base import AdapterUnavailable
from cn_altdata_brief.config import SourceConfig, load_source_config

FIXTURES = Path(__file__).parent / "fixtures"
PUBLIC_FIXTURES = FIXTURES / "public_summary"
SP_PUBLIC = PUBLIC_FIXTURES / "alt_data_summary.json"
IX_PUBLIC = PUBLIC_FIXTURES / "index_research_summary.json"


# ----------------------------------------------------------------------
# Super-pricing
# ----------------------------------------------------------------------


class TestSuperPricingPublicSummary:
    def test_prefers_public_when_both_present(
        self, super_pricing_cache: Path
    ) -> None:
        adapter = SuperPricingAdapter(
            cache_dir=super_pricing_cache,
            public_summary=SP_PUBLIC,
            config=load_source_config(preference="auto"),
        )
        payload = adapter.fetch()
        assert payload.data["source_mode"] == "public"
        assert payload.data["schema_version"] == 1
        # The cache fixture has policy_count=12 but the public fixture has 20;
        # if we picked public, we should see 20.
        assert payload.data["policy_radar"]["policy_count"] == 20

    def test_falls_back_to_cache_when_public_missing(
        self, super_pricing_cache: Path, tmp_path: Path
    ) -> None:
        nonexistent = tmp_path / "no_such.json"
        adapter = SuperPricingAdapter(
            cache_dir=super_pricing_cache,
            public_summary=nonexistent,
            config=load_source_config(preference="auto"),
        )
        payload = adapter.fetch()
        assert payload.data["source_mode"] == "cache"
        # Cache fixture has policy_count=12.
        assert payload.data["policy_radar"]["policy_count"] == 12

    def test_public_only_missing_raises_unavailable(
        self, super_pricing_cache: Path, tmp_path: Path
    ) -> None:
        adapter = SuperPricingAdapter(
            cache_dir=super_pricing_cache,
            public_summary=tmp_path / "absent.json",
            config=SourceConfig(preference="public_only"),
        )
        with pytest.raises(AdapterUnavailable) as exc_info:
            adapter.fetch()
        assert "public summary" in str(exc_info.value).lower()
        assert "public_only" in str(exc_info.value)

    def test_cache_only_ignores_public(
        self, super_pricing_cache: Path
    ) -> None:
        adapter = SuperPricingAdapter(
            cache_dir=super_pricing_cache,
            public_summary=SP_PUBLIC,
            config=SourceConfig(preference="cache_only"),
        )
        payload = adapter.fetch()
        assert payload.data["source_mode"] == "cache"

    def test_public_schema_maps_to_internal_shape(self) -> None:
        adapter = SuperPricingAdapter(
            cache_dir=Path("/dev/null/does-not-exist"),
            public_summary=SP_PUBLIC,
            config=SourceConfig(preference="public_only"),
        )
        payload = adapter.fetch()
        policy = payload.data["policy_radar"]
        macro = payload.data["macro_hf"]
        # Same keys as cache-shape — synthesis layer cares about these.
        assert "industry_signals" in policy
        assert "policy_count" in policy
        # industry_signals must be the ranked list-of-dicts form.
        assert isinstance(policy["industry_signals"], list)
        assert policy["industry_signals"][0]["industry"] == "新能源汽车"
        assert policy["industry_signals"][0]["signal"] == "bearish"
        # macro metals mapped from per-metal dict to list-of-dicts.
        assert isinstance(macro["metals"], list)
        names = {m["metal"] for m in macro["metals"]}
        assert {"copper", "aluminium", "nickel"} <= names
        # Chinese names looked up via the metal mapping.
        cn_names = {m["name_cn"] for m in macro["metals"]}
        assert "铜" in cn_names

    def test_both_absent_raises(self, tmp_path: Path) -> None:
        adapter = SuperPricingAdapter(
            cache_dir=tmp_path / "no_cache",
            public_summary=tmp_path / "no_public.json",
            config=load_source_config(preference="auto"),
        )
        with pytest.raises(AdapterUnavailable):
            adapter.fetch()


# ----------------------------------------------------------------------
# Index research
# ----------------------------------------------------------------------


class TestIndexResearchPublicSummary:
    def test_prefers_public_when_both_present(
        self, index_research_tables: Path
    ) -> None:
        adapter = IndexResearchAdapter(
            table_dir=index_research_tables,
            figure_dir=index_research_tables,
            public_summary=IX_PUBLIC,
            config=load_source_config(preference="auto"),
        )
        payload = adapter.fetch()
        assert payload.data["source_mode"] == "public"
        # The fixture exposes hs300_rdd metadata that the CSV path never has.
        assert payload.data["hs300_rdd"]["n_obs"] == 156
        assert payload.data["sensitivity"]["H5"]["core_p"] == 0.0082

    def test_falls_back_to_cache_when_public_missing(
        self, index_research_tables: Path, tmp_path: Path
    ) -> None:
        adapter = IndexResearchAdapter(
            table_dir=index_research_tables,
            figure_dir=index_research_tables,
            public_summary=tmp_path / "absent.json",
            config=load_source_config(preference="auto"),
        )
        payload = adapter.fetch()
        assert payload.data["source_mode"] == "cache"
        assert len(payload.data["verdicts"]) == 7

    def test_public_only_missing_raises(
        self, index_research_tables: Path, tmp_path: Path
    ) -> None:
        adapter = IndexResearchAdapter(
            table_dir=index_research_tables,
            figure_dir=index_research_tables,
            public_summary=tmp_path / "absent.json",
            config=SourceConfig(preference="public_only"),
        )
        with pytest.raises(AdapterUnavailable) as exc_info:
            adapter.fetch()
        assert "public_only" in str(exc_info.value)

    def test_public_schema_maps_to_internal_shape(
        self, index_research_tables: Path
    ) -> None:
        adapter = IndexResearchAdapter(
            table_dir=index_research_tables,
            figure_dir=index_research_tables,
            public_summary=IX_PUBLIC,
            config=SourceConfig(preference="public_only"),
        )
        payload = adapter.fetch()
        verdicts = payload.data["verdicts"]
        assert len(verdicts) == 7
        # Each row carries the same key set as the CSV path.
        keys = set(verdicts[0].keys())
        expected_keys = {
            "hid",
            "name_cn",
            "verdict",
            "confidence",
            "key_label",
            "key_value",
            "p_value",
            "n_obs",
            "track",
            "evidence_tier",
        }
        assert expected_keys <= keys
        # PAP filter to "changed" rows works the same way.
        changes = payload.data["pap_changes"]
        assert len(changes) == 1
        assert changes[0]["hid"] == "H2"

    def test_dict_of_rows_verdict_shape(self, tmp_path: Path) -> None:
        """The real index-research public summary uses dict[hid -> row]
        with a ``headline_metric`` string instead of broken-out fields.
        """
        import json

        public_path = tmp_path / "ix.json"
        public_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "generated_at": "2026-05-17T02:00:00+00:00",
                    "verdicts": {
                        "H1": {
                            "name": "信息泄露与预运行",
                            "verdict": "证据不足",
                            "confidence": "中",
                            "evidence_tier": "core",
                            "track": "identification",
                            "n_obs": 436,
                            "headline_metric": "bootstrap p = 0.8748",
                        }
                    },
                    "pap_deviation_summary": {"all_unchanged": True},
                }
            ),
            encoding="utf-8",
        )
        adapter = IndexResearchAdapter(
            table_dir=tmp_path / "no_table",
            figure_dir=tmp_path / "no_fig",
            public_summary=public_path,
            config=SourceConfig(preference="public_only"),
        )
        payload = adapter.fetch()
        v = payload.data["verdicts"]
        assert len(v) == 1
        assert v[0]["hid"] == "H1"
        assert v[0]["name_cn"] == "信息泄露与预运行"
        # headline_metric parsed into key_label / key_value / p_value.
        assert v[0]["key_label"] == "bootstrap p"
        assert v[0]["p_value"] == pytest.approx(0.8748)
        assert v[0]["key_value"] == pytest.approx(0.8748)
        assert payload.data["pap_changes"] == []


# ----------------------------------------------------------------------
# Config integration
# ----------------------------------------------------------------------


class TestSourceConfigEnv:
    def test_env_var_drives_preference(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CN_ALTDATA_BRIEF_PREFERENCE", "public_only")
        cfg = load_source_config()
        assert cfg.preference == "public_only"
        assert cfg.public_required is True
        assert cfg.allow_cache is False

    def test_explicit_preference_overrides_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CN_ALTDATA_BRIEF_PREFERENCE", "public_only")
        cfg = load_source_config(preference="cache_only")
        assert cfg.preference == "cache_only"
        assert cfg.allow_cache is True
        assert cfg.public_required is False

    def test_invalid_preference_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        with pytest.raises(ValueError):
            load_source_config(preference="garbage")
