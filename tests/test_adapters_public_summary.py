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

from altdata_brief.adapters import (
    ETF512400Adapter,
    IndexResearchAdapter,
    QuantTradingAdapter,
    SuperPricingAdapter,
)
from altdata_brief.adapters.base import AdapterUnavailable
from altdata_brief.adapters.schema import (
    MissingSchemaVersionError,
    SchemaContract,
    resolve_schema_version,
)
from altdata_brief.config import SourceConfig, load_source_config

FIXTURES = Path(__file__).parent / "fixtures"
PUBLIC_FIXTURES = FIXTURES / "public_summary"
SP_PUBLIC = PUBLIC_FIXTURES / "alt_data_summary.json"
IX_PUBLIC = PUBLIC_FIXTURES / "index_research_summary.json"
QT_PUBLIC = PUBLIC_FIXTURES / "quant_summary.json"
# The ETF "public summary" is the JS app's liveSnapshot — same file used as cache.
ETF_PUBLIC = FIXTURES / "etf_512400" / "liveSnapshot.json"


# ----------------------------------------------------------------------
# Schema-version dispatch
# ----------------------------------------------------------------------


class TestSchemaVersionDispatch:
    def test_missing_version_uses_declared_implicit_default(self) -> None:
        contract = SchemaContract(
            source="etf_512400",
            supported=frozenset({1}),
            implicit_version=1,
        )

        assert resolve_schema_version({}, contract) == 1

    def test_explicit_unusable_version_does_not_use_implicit_default(self) -> None:
        contract = SchemaContract(
            source="etf_512400",
            supported=frozenset({1}),
            implicit_version=1,
        )

        with pytest.raises(MissingSchemaVersionError) as exc_info:
            resolve_schema_version({"schema_version": "v-next"}, contract)

        assert "schema_version" in str(exc_info.value)
        assert "v-next" in str(exc_info.value)


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

    def test_public_summary_figure_links_ignore_path_traversal(
        self, tmp_path: Path
    ) -> None:
        """Figure metadata is public JSON, so paths are sanitized to basenames."""
        import json

        figure_dir = tmp_path / "figures"
        figure_dir.mkdir()
        (figure_dir / "safe.png").write_bytes(b"safe")
        (figure_dir / "published.png").write_bytes(b"published")
        outside = tmp_path / "outside.png"
        outside.write_bytes(b"outside")

        public_path = tmp_path / "ix.json"
        public_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "generated_at": "2026-05-17T02:00:00+00:00",
                    "verdicts": [],
                    "figures": [
                        "safe.png",
                        "../outside.png",
                        str(outside.resolve()),
                    ],
                    "figures_published": [
                        "results/figures/published.png",
                        "../figures/safe.png",
                    ],
                }
            ),
            encoding="utf-8",
        )

        adapter = IndexResearchAdapter(
            table_dir=tmp_path / "no_table",
            figure_dir=figure_dir,
            public_summary=public_path,
            config=SourceConfig(preference="public_only"),
        )
        payload = adapter.fetch()

        assert payload.files == [figure_dir / "safe.png", figure_dir / "published.png"]
        assert payload.data["figure_links"] == [
            str(figure_dir / "safe.png"),
            str(figure_dir / "published.png"),
        ]


# ----------------------------------------------------------------------
# Quant trading (v0.4)
# ----------------------------------------------------------------------


class TestQuantTradingPublicSummary:
    def test_prefers_public_when_both_present(
        self, quant_trading_cache: Path
    ) -> None:
        adapter = QuantTradingAdapter(
            cache_dir=quant_trading_cache,
            public_summary=QT_PUBLIC,
            config=load_source_config(preference="auto"),
        )
        payload = adapter.fetch()
        assert payload.data["source_mode"] == "public"
        # Public fixture has policy_count=9 too — but it carries an explicit
        # heat ranking the cache fallback can't produce.
        industries = payload.data["industries"]
        assert industries[0]["industry"] == "有色金属"
        assert industries[0]["heat_score"] == pytest.approx(0.873)
        # ETF rotation context surfaced.
        rotation = payload.data["etf_rotation"]
        assert rotation["audit_count"] == 14
        assert rotation["strategy_count"] == 3
        # paper_trading metadata when present.
        assert payload.data["paper_trading"]["open_positions"] == 2

    def test_falls_back_to_cache_when_public_missing(
        self, quant_trading_cache: Path, tmp_path: Path
    ) -> None:
        adapter = QuantTradingAdapter(
            cache_dir=quant_trading_cache,
            public_summary=tmp_path / "absent.json",
            config=load_source_config(preference="auto"),
        )
        payload = adapter.fetch()
        assert payload.data["source_mode"] == "cache"
        # Cache fixture has 新能源汽车 dominating mentions.
        rows = payload.data["industries"]
        assert rows[0]["industry"] == "新能源汽车"

    def test_public_only_missing_raises(
        self, quant_trading_cache: Path, tmp_path: Path
    ) -> None:
        adapter = QuantTradingAdapter(
            cache_dir=quant_trading_cache,
            public_summary=tmp_path / "absent.json",
            config=SourceConfig(preference="public_only"),
        )
        with pytest.raises(AdapterUnavailable) as exc_info:
            adapter.fetch()
        assert "public_only" in str(exc_info.value)

    def test_cache_only_ignores_public(self, quant_trading_cache: Path) -> None:
        adapter = QuantTradingAdapter(
            cache_dir=quant_trading_cache,
            public_summary=QT_PUBLIC,
            config=SourceConfig(preference="cache_only"),
        )
        payload = adapter.fetch()
        assert payload.data["source_mode"] == "cache"

    def test_public_schema_falls_back_to_policy_top_industries(
        self, quant_trading_cache: Path, tmp_path: Path
    ) -> None:
        """When industry_heat is absent we derive heat from policy_radar."""
        import json

        path = tmp_path / "quant_no_heat.json"
        path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "generated_at": "2026-05-17T02:00:00+00:00",
                    "providers": {
                        "policy_radar": {
                            "policy_count": 5,
                            "top_industries": [
                                {"industry": "AI算力", "avg_impact": 0.2,
                                 "mentions": 30, "signal": "bullish"},
                                {"industry": "电网", "avg_impact": 0.05,
                                 "mentions": 12, "signal": "neutral"},
                            ],
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        adapter = QuantTradingAdapter(
            cache_dir=quant_trading_cache,
            public_summary=path,
            config=SourceConfig(preference="public_only"),
        )
        payload = adapter.fetch()
        rows = payload.data["industries"]
        assert len(rows) == 2
        # AI算力 wins on both mentions AND impact.
        assert rows[0]["industry"] == "AI算力"
        assert rows[0]["heat_score"] > rows[1]["heat_score"]
        assert payload.data["source_mode"] == "public"


# ----------------------------------------------------------------------
# ETF 512400 (v0.4 — public-by-default)
# ----------------------------------------------------------------------


class TestETF512400PublicSummary:
    def test_public_by_default_uses_same_file(self, etf_512400_snapshot: Path) -> None:
        """The JS app commits its snapshot; we report public_mode without copying."""
        adapter = ETF512400Adapter(
            snapshot_path=etf_512400_snapshot,
            public_summary=etf_512400_snapshot,
            config=load_source_config(preference="auto"),
        )
        payload = adapter.fetch()
        assert payload.data["source_mode"] == "public"
        assert payload.data["public_by_default"] is True
        # Same on-disk artifact regardless of preference.
        assert payload.cache_path == etf_512400_snapshot

    def test_cache_mode_reports_cache_source_mode(
        self, etf_512400_snapshot: Path
    ) -> None:
        adapter = ETF512400Adapter(
            snapshot_path=etf_512400_snapshot,
            public_summary=etf_512400_snapshot,
            config=SourceConfig(preference="cache_only"),
        )
        payload = adapter.fetch()
        assert payload.data["source_mode"] == "cache"
        assert payload.data["public_by_default"] is True  # still public-by-default

    def test_public_only_missing_raises(self, tmp_path: Path) -> None:
        absent = tmp_path / "missing_liveSnapshot.json"
        adapter = ETF512400Adapter(
            snapshot_path=absent,
            public_summary=absent,
            config=SourceConfig(preference="public_only"),
        )
        with pytest.raises(AdapterUnavailable) as exc_info:
            adapter.fetch()
        # The error must hint that npm run refresh is the fix.
        assert "npm run refresh" in str(exc_info.value).lower() or \
               "public-by-default" in str(exc_info.value).lower()

    def test_resolve_source_returns_public(self, etf_512400_snapshot: Path) -> None:
        adapter = ETF512400Adapter(
            snapshot_path=etf_512400_snapshot,
            public_summary=etf_512400_snapshot,
            config=load_source_config(preference="auto"),
        )
        res = adapter.resolve_source()
        assert res.mode == "public"
        assert res.available is True
        assert res.path == etf_512400_snapshot
        assert res.mtime_iso is not None


# ----------------------------------------------------------------------
# Cross-adapter resolution probe
# ----------------------------------------------------------------------


class TestResolveSource:
    def test_super_pricing_resolve_returns_public_when_present(
        self, super_pricing_cache: Path
    ) -> None:
        adapter = SuperPricingAdapter(
            cache_dir=super_pricing_cache,
            public_summary=SP_PUBLIC,
            config=load_source_config(preference="auto"),
        )
        res = adapter.resolve_source()
        assert res.mode == "public"
        assert res.available is True
        assert res.path == SP_PUBLIC

    def test_super_pricing_resolve_falls_to_cache(
        self, super_pricing_cache: Path, tmp_path: Path
    ) -> None:
        adapter = SuperPricingAdapter(
            cache_dir=super_pricing_cache,
            public_summary=tmp_path / "missing.json",
            config=load_source_config(preference="auto"),
        )
        res = adapter.resolve_source()
        assert res.mode == "cache"
        assert res.available is True

    def test_resolve_missing_when_both_absent(self, tmp_path: Path) -> None:
        adapter = SuperPricingAdapter(
            cache_dir=tmp_path / "nope",
            public_summary=tmp_path / "nope.json",
            config=load_source_config(preference="auto"),
        )
        res = adapter.resolve_source()
        assert res.mode == "missing"
        assert res.available is False


# ----------------------------------------------------------------------
# Config integration
# ----------------------------------------------------------------------


class TestSourceConfigEnv:
    def test_env_var_drives_preference(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ALTDATA_BRIEF_PREFERENCE", "public_only")
        cfg = load_source_config()
        assert cfg.preference == "public_only"
        assert cfg.public_required is True
        assert cfg.allow_cache is False

    def test_public_summary_preference_alias_drives_preference(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PUBLIC_SUMMARY_PREFERENCE", "public_only")
        cfg = load_source_config()
        assert cfg.preference == "public_only"
        assert cfg.public_required is True
        assert cfg.allow_cache is False

    def test_cn_preference_overrides_public_summary_alias(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PUBLIC_SUMMARY_PREFERENCE", "public_only")
        monkeypatch.setenv("ALTDATA_BRIEF_PREFERENCE", "cache_only")
        cfg = load_source_config()
        assert cfg.preference == "cache_only"
        assert cfg.allow_cache is True
        assert cfg.public_required is False

    def test_legacy_cn_preference_still_works(
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
        monkeypatch.setenv("ALTDATA_BRIEF_PREFERENCE", "public_only")
        cfg = load_source_config(preference="cache_only")
        assert cfg.preference == "cache_only"
        assert cfg.allow_cache is True
        assert cfg.public_required is False

    def test_invalid_preference_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        with pytest.raises(ValueError):
            load_source_config(preference="garbage")
