"""Tests for the v0.12+ content-quality validate checks.

Covers the quality check functions in ``validate_quality.py`` plus
their CLI integration via the ``--strict`` flag on ``validate``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cn_altdata_brief import cli as cli_mod
from cn_altdata_brief import validate_quality as vq
from cn_altdata_brief.adapters.base import AdapterPayload
from cn_altdata_brief.cli import main
from cn_altdata_brief.validate import (
    EXIT_FAIL,
    EXIT_OK,
    EXIT_WARN,
    FAIL,
    INFO,
    WARN,
)

# ---------------------------------------------------------------------------
# Payload factories — keep the per-test surface compact.
# ---------------------------------------------------------------------------


def _payload(source: str, data: dict) -> AdapterPayload:
    return AdapterPayload(
        source=source,
        fetched_at="t",
        cache_path=None,
        live=False,
        data=data,
    )


def _super_pricing_payload(
    *,
    signals: dict[str, dict] | list[dict] | None = None,
    metals: list[dict] | None = None,
) -> AdapterPayload:
    """Synthetic super_pricing payload mirroring the adapter's output shape."""
    return _payload(
        "super_pricing",
        {
            "policy_radar": {
                "industry_signals": signals if signals is not None else [],
                "policy_count": 0,
            },
            "macro_hf": {"metals": metals if metals is not None else []},
        },
    )


def _quant_payload(industries: list[dict] | None = None) -> AdapterPayload:
    return _payload(
        "quant_trading",
        {
            "industries": industries if industries is not None else [],
            "policy_count": 0,
        },
    )


# ---------------------------------------------------------------------------
# 1. Fingerprint detection
# ---------------------------------------------------------------------------


def test_fingerprint_warns_after_two_identical_days(tmp_path: Path) -> None:
    """Run the same fingerprint 3 days in a row → WARN once we cross stale_after_days."""
    history_path = tmp_path / "fp.json"
    payloads = {
        "super_pricing": _super_pricing_payload(
            signals={
                "新能源汽车": {"avg_impact": -0.4, "mentions": 50, "signal": "bearish"},
                "电网": {"avg_impact": 0.05, "mentions": 4, "signal": "neutral"},
            }
        )
    }
    # Day 1: fresh content, no history yet.
    r1 = vq.check_content_fingerprint_freshness(
        payloads, history_path=history_path, today="2026-05-17"
    )
    assert r1.level == INFO
    # Day 2: same content, run number 2 → consecutive_days=2, still inside threshold.
    r2 = vq.check_content_fingerprint_freshness(
        payloads, history_path=history_path, today="2026-05-18"
    )
    assert r2.level == INFO
    # Day 3: same content again → consecutive_days=3 > threshold=2 → WARN.
    r3 = vq.check_content_fingerprint_freshness(
        payloads,
        history_path=history_path,
        today="2026-05-19",
        stale_after_days=2,
    )
    assert r3.level == WARN
    assert "unchanged" in r3.message
    # History should reflect a single rolling entry whose last_seen advanced.
    history = vq.load_fingerprint_history(history_path)
    assert len(history["super_pricing"]) == 1
    assert history["super_pricing"][0].first_seen == "2026-05-17"
    assert history["super_pricing"][0].last_seen == "2026-05-19"


def test_fingerprint_new_signals_break_the_streak(tmp_path: Path) -> None:
    """Different content on day 2 → new entry, consecutive_days resets to 1."""
    history_path = tmp_path / "fp.json"
    day1 = {"super_pricing": _super_pricing_payload(
        signals={"AI算力": {"avg_impact": 0.3, "mentions": 5, "signal": "bullish"}}
    )}
    day2 = {"super_pricing": _super_pricing_payload(
        signals={"AI算力": {"avg_impact": 0.6, "mentions": 8, "signal": "bullish"}}
    )}
    vq.check_content_fingerprint_freshness(
        day1, history_path=history_path, today="2026-05-17"
    )
    r = vq.check_content_fingerprint_freshness(
        day2, history_path=history_path, today="2026-05-18", stale_after_days=2
    )
    assert r.level == INFO
    history = vq.load_fingerprint_history(history_path)
    assert len(history["super_pricing"]) == 2


# ---------------------------------------------------------------------------
# 2. Signal density
# ---------------------------------------------------------------------------


def test_signal_density_all_zero_impact_warns() -> None:
    """10 records all with |avg_impact|<=0.1 → density 0% → WARN."""
    payloads = {
        "super_pricing": _super_pricing_payload(
            signals=[
                {"industry": f"行业{i}", "avg_impact": 0.0, "mentions": 1, "signal": "neutral"}
                for i in range(10)
            ],
            metals=[
                {"metal": "copper", "name_cn": "铜", "price_change_pct": 1.2},
                {"metal": "aluminium", "name_cn": "铝", "price_change_pct": -0.8},
            ],
        )
    }
    r = vq.check_signal_density(payloads)
    assert r.level == WARN
    assert "policy_radar density" in r.message
    assert r.detail is not None
    assert r.detail["policy_radar"]["with_signal"] == 0


def test_signal_density_healthy_passes() -> None:
    """Mix of signal-carrying rows above 30% threshold → INFO."""
    payloads = {
        "super_pricing": _super_pricing_payload(
            signals=[
                {"industry": "A", "avg_impact": 0.5, "mentions": 5, "signal": "bullish"},
                {"industry": "B", "avg_impact": -0.4, "mentions": 4, "signal": "bearish"},
                {"industry": "C", "avg_impact": 0.3, "mentions": 2, "signal": "bullish"},
                {"industry": "D", "avg_impact": 0.05, "mentions": 1, "signal": "neutral"},
            ],
            metals=[
                {"metal": "copper", "name_cn": "铜", "price_change_pct": 1.5},
                {"metal": "aluminium", "name_cn": "铝", "price_change_pct": -0.5},
            ],
        )
    }
    r = vq.check_signal_density(payloads)
    assert r.level == INFO
    assert "healthy" in r.message


def test_signal_density_macro_all_zero_warns() -> None:
    """Healthy policy rows but all-zero metals → still WARN (worst wins)."""
    payloads = {
        "super_pricing": _super_pricing_payload(
            signals=[
                {"industry": "A", "avg_impact": 0.5, "mentions": 5, "signal": "bullish"},
                {"industry": "B", "avg_impact": -0.4, "mentions": 4, "signal": "bearish"},
            ],
            metals=[
                {"metal": "copper", "name_cn": "铜", "price_change_pct": 0.0},
                {"metal": "aluminium", "name_cn": "铝", "price_change_pct": 0.0},
                {"metal": "nickel", "name_cn": "镍", "price_change_pct": 0.0},
            ],
        )
    }
    r = vq.check_signal_density(payloads)
    assert r.level == WARN
    assert "macro_hf density" in r.message


# ---------------------------------------------------------------------------
# 3. Cross-source consistency
# ---------------------------------------------------------------------------


def test_cross_source_conflict_detected() -> None:
    """policy says bullish, quant says bearish for the same industry → WARN."""
    payloads = {
        "super_pricing": _super_pricing_payload(
            signals={"新能源": {"avg_impact": 0.5, "mentions": 8, "signal": "bullish"}}
        ),
        "quant_trading": _quant_payload(
            industries=[
                {
                    "industry": "新能源",
                    "heat_score": 0.8,
                    "policy_signal": "bearish",
                    "policy_impact": -0.2,
                    "mentions": 3,
                }
            ]
        ),
    }
    r = vq.check_cross_source_consistency(payloads)
    assert r.level == WARN
    assert "新能源" in r.message
    assert r.detail is not None
    assert len(r.detail["conflicts"]) == 1


def test_cross_source_agreement_passes() -> None:
    """Both sources say bullish → INFO, conflicts empty."""
    payloads = {
        "super_pricing": _super_pricing_payload(
            signals={"AI算力": {"avg_impact": 0.4, "mentions": 6, "signal": "bullish"}}
        ),
        "quant_trading": _quant_payload(
            industries=[
                {
                    "industry": "AI算力",
                    "heat_score": 0.7,
                    "policy_signal": "bullish",
                    "policy_impact": 0.3,
                    "mentions": 5,
                }
            ]
        ),
    }
    r = vq.check_cross_source_consistency(payloads)
    assert r.level == INFO
    assert r.detail is not None
    assert r.detail["conflicts"] == []
    assert len(r.detail["agreements"]) == 1


def test_cross_source_no_overlap_is_info() -> None:
    """Sources name different industries → nothing to compare → INFO."""
    payloads = {
        "super_pricing": _super_pricing_payload(
            signals={"新能源汽车": {"avg_impact": -0.4, "mentions": 50, "signal": "bearish"}}
        ),
        "quant_trading": _quant_payload(
            industries=[
                {
                    "industry": "测试行业",
                    "heat_score": 0.5,
                    "policy_signal": "neutral",
                    "policy_impact": 0.0,
                    "mentions": 0,
                }
            ]
        ),
    }
    r = vq.check_cross_source_consistency(payloads)
    assert r.level == INFO
    assert "nothing to compare" in r.message


# ---------------------------------------------------------------------------
# 4. Schema regression
# ---------------------------------------------------------------------------


def _write_minimal_schemas(schema_dir: Path) -> None:
    schema_dir.mkdir(parents=True, exist_ok=True)
    (schema_dir / "super_pricing.schema.json").write_text(
        json.dumps(
            {
                "baseline_version": 1,
                "source": "super_pricing",
                "expected_payload_keys": {
                    "policy_radar": {
                        "required": ["industry_signals", "policy_count"],
                        "optional": [],
                    }
                },
            }
        ),
        encoding="utf-8",
    )


def test_schema_missing_required_fails(tmp_path: Path) -> None:
    """Drop a required key from the payload → FAIL with the key name."""
    _write_minimal_schemas(tmp_path)
    payload = _payload(
        "super_pricing",
        {"policy_radar": {"industry_signals": []}},  # policy_count missing
    )
    payloads = {"super_pricing": payload}
    r = vq.check_schema_regression(payloads, schema_dir=tmp_path)
    assert r.level == FAIL
    assert "policy_count" in r.message
    assert r.detail is not None
    assert any(
        "policy_count" in k
        for k in r.detail["per_source"]["super_pricing"]["missing_required"]
    )


def test_schema_unknown_keys_info(tmp_path: Path) -> None:
    """Extra keys not in baseline → INFO (schema evolution, not blocker)."""
    _write_minimal_schemas(tmp_path)
    payload = _payload(
        "super_pricing",
        {
            "policy_radar": {
                "industry_signals": [],
                "policy_count": 0,
                "brand_new_field": 42,
            }
        },
    )
    payloads = {"super_pricing": payload}
    r = vq.check_schema_regression(payloads, schema_dir=tmp_path)
    assert r.level == INFO
    assert "brand_new_field" in r.message


# ---------------------------------------------------------------------------
# 5. Required upstream path audit
# ---------------------------------------------------------------------------


def _write_summary(path: Path, data: dict) -> Path:
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_required_paths_missing_nested_source_fails_with_path(tmp_path: Path) -> None:
    """Raw upstream drift should FAIL and name the missing dotted path."""
    summary = _write_summary(
        tmp_path / "sp.json",
        {
            "schema_version": 1,
            "providers": {
                "policy_radar": {
                    "industry_signals": {"电网": {"signal": "bullish"}},
                    "policy_count": 1,
                },
                "macro_hf": {},
            },
        },
    )

    r = vq.check_required_paths({}, summary_paths={"super_pricing": summary})

    assert r.level == FAIL
    assert "providers.macro_hf.metals" in r.message
    assert r.detail is not None
    missing = r.detail["per_source"]["super_pricing"]["missing_paths"]
    assert "providers.macro_hf.metals" in missing


def test_required_paths_wrong_type_container_fails_with_path(tmp_path: Path) -> None:
    """A required upstream container present as a scalar is still unusable."""
    summary = _write_summary(
        tmp_path / "sp.json",
        {
            "schema_version": 1,
            "providers": {
                "policy_radar": {
                    "industry_signals": {"电网": {"signal": "bullish"}},
                    "policy_count": 1,
                },
                "macro_hf": {"metals": "unavailable"},
            },
        },
    )

    r = vq.check_required_paths({}, summary_paths={"super_pricing": summary})

    assert r.level == FAIL
    assert "providers.macro_hf.metals" in r.message
    assert r.detail is not None
    entry = r.detail["per_source"]["super_pricing"]
    assert "providers.macro_hf.metals" in entry["missing_paths"]
    assert entry["invalid_type_paths"]["providers.macro_hf.metals"] == "str"


def test_required_paths_wrong_type_scalar_fails_with_path(tmp_path: Path) -> None:
    """A required upstream scalar present as a container is still unusable."""
    summary = _write_summary(
        tmp_path / "sp.json",
        {
            "schema_version": 1,
            "providers": {
                "policy_radar": {
                    "industry_signals": {"电网": {"signal": "bullish"}},
                    "policy_count": {"count": 1},
                },
                "macro_hf": {"metals": [{"metal": "copper"}]},
            },
        },
    )

    r = vq.check_required_paths({}, summary_paths={"super_pricing": summary})

    assert r.level == FAIL
    assert "providers.policy_radar.policy_count" in r.message
    assert r.detail is not None
    entry = r.detail["per_source"]["super_pricing"]
    assert "providers.policy_radar.policy_count" in entry["missing_paths"]
    assert entry["invalid_type_paths"]["providers.policy_radar.policy_count"] == "dict"


def test_required_paths_quant_accepts_documented_heat_fallback(tmp_path: Path) -> None:
    """Quant can source heat from policy_radar.industry_signals when industry_heat is absent."""
    summary = _write_summary(
        tmp_path / "quant.json",
        {
            "schema_version": 1,
            "providers": {
                "policy_radar": {
                    "policy_count": 1,
                    "industry_signals": {"电网": {"signal": "bullish"}},
                }
            },
        },
    )

    r = vq.check_required_paths({}, summary_paths={"quant_trading": summary})

    assert r.level == INFO
    assert r.detail is not None
    assert r.detail["per_source"]["quant_trading"]["status"] == "ok"


def test_required_paths_quant_fails_when_all_heat_sources_missing(tmp_path: Path) -> None:
    """The any-of group should fail only when every documented heat source is absent."""
    summary = _write_summary(
        tmp_path / "quant.json",
        {
            "schema_version": 1,
            "providers": {
                "policy_radar": {
                    "policy_count": 1,
                }
            },
        },
    )

    r = vq.check_required_paths({}, summary_paths={"quant_trading": summary})

    assert r.level == FAIL
    assert "top_industries_by_score" in r.message
    assert "none present" in r.message


def test_required_paths_cache_payload_does_not_audit_default_summary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cache/synthetic payloads must not read unrelated sibling public summaries."""
    stale_summary = _write_summary(
        tmp_path / "unrelated_public_summary.json",
        {"schema_version": 1, "providers": {"macro_hf": {}}},
    )
    from cn_altdata_brief import config as config_mod

    monkeypatch.setattr(
        config_mod,
        "public_summary_path",
        lambda source_key: stale_summary,
    )

    payloads = {
        "super_pricing": _payload(
            "super_pricing",
            {
                "source_mode": "cache",
                "policy_radar": {"industry_signals": []},
                "macro_hf": {"metals": []},
            },
        )
    }

    r = vq.check_required_paths(payloads)

    assert r.level == INFO
    assert r.detail is not None
    assert r.detail["audited_sources"] == 0
    assert r.detail["per_source"]["super_pricing"]["status"] == "skipped_no_summary"


def test_required_paths_audits_explicit_payload_public_summary(tmp_path: Path) -> None:
    """A payload-stamped public summary path is still the source of truth."""
    summary = _write_summary(
        tmp_path / "explicit_public_summary.json",
        {"schema_version": 1, "providers": {"macro_hf": {}}},
    )
    payloads = {
        "super_pricing": _payload(
            "super_pricing",
            {"source_mode": "public", "public_summary_path": str(summary)},
        )
    }

    r = vq.check_required_paths(payloads)

    assert r.level == FAIL
    assert r.detail is not None
    assert r.detail["audited_sources"] == 1
    assert r.detail["per_source"]["super_pricing"]["path"] == str(summary)
    missing = r.detail["per_source"]["super_pricing"]["missing_paths"]
    assert "providers.macro_hf.metals" in missing


def test_required_paths_warns_when_payload_stamped_summary_path_is_missing(
    tmp_path: Path,
) -> None:
    """A disappeared public summary consumed by the adapter must not be silently skipped."""
    missing_summary = tmp_path / "missing_public_summary.json"
    payloads = {
        "super_pricing": _payload(
            "super_pricing",
            {
                "source_mode": "public",
                "public_summary_path": str(missing_summary),
            },
        )
    }

    r = vq.check_required_paths(payloads)

    assert r.level == WARN
    assert "unreadable summary" in r.message
    assert r.detail is not None
    entry = r.detail["per_source"]["super_pricing"]
    assert entry["status"] == "parse_error"
    assert entry["path"] == str(missing_summary)


def test_required_paths_warns_when_explicit_summary_path_is_missing(
    tmp_path: Path,
) -> None:
    """Explicit contract-test targets should warn instead of being skipped."""
    missing_summary = tmp_path / "missing_public_summary.json"

    r = vq.check_required_paths(
        {},
        summary_paths={"super_pricing": missing_summary},
    )

    assert r.level == WARN
    assert "unreadable summary" in r.message
    assert r.detail is not None
    entry = r.detail["per_source"]["super_pricing"]
    assert entry["status"] == "parse_error"
    assert entry["path"] == str(missing_summary)


def test_required_paths_fail_message_keeps_unreadable_summary_context(
    tmp_path: Path,
) -> None:
    """Combined FAILs should still name unreadable summary artifacts."""
    bad_super_pricing = _write_summary(
        tmp_path / "sp.json",
        {
            "schema_version": 1,
            "providers": {
                "policy_radar": {
                    "industry_signals": {"电网": {"signal": "bullish"}},
                    "policy_count": 1,
                },
                "macro_hf": {},
            },
        },
    )
    missing_quant = tmp_path / "missing_quant.json"

    r = vq.check_required_paths(
        {},
        summary_paths={
            "super_pricing": bad_super_pricing,
            "quant_trading": missing_quant,
        },
    )

    assert r.level == FAIL
    assert "providers.macro_hf.metals" in r.message
    assert "quant_trading: unreadable summary" in r.message
    assert r.detail is not None
    assert r.detail["per_source"]["quant_trading"]["status"] == "parse_error"


# ---------------------------------------------------------------------------
# 6. All-pass case across all strict checks
# ---------------------------------------------------------------------------


def test_strict_all_pass_on_diverse_data(tmp_path: Path) -> None:
    """Healthy payload with diverse signals → every strict check OK."""
    _write_minimal_schemas(tmp_path)
    payloads = {
        "super_pricing": _super_pricing_payload(
            signals=[
                {"industry": "A", "avg_impact": 0.5, "mentions": 5, "signal": "bullish"},
                {"industry": "B", "avg_impact": -0.4, "mentions": 4, "signal": "bearish"},
            ],
            metals=[
                {"metal": "copper", "name_cn": "铜", "price_change_pct": 1.5},
                {"metal": "aluminium", "name_cn": "铝", "price_change_pct": -0.5},
            ],
        ),
        "quant_trading": _quant_payload(
            industries=[
                {
                    "industry": "A",
                    "heat_score": 0.8,
                    "policy_signal": "bullish",
                    "policy_impact": 0.4,
                    "mentions": 3,
                }
            ]
        ),
    }
    results = vq.run_strict_checks(
        payloads,
        history_path=tmp_path / "fp.json",
        schema_dir=tmp_path,
        today="2026-05-19",
    )
    levels = {r.name: r.level for r in results}
    # fingerprint, density, consistency all INFO; schema INFO (only super_pricing baseline loaded);
    # required_paths INFO-skips because no raw public summaries were injected.
    assert levels["content_fingerprint_freshness"] == INFO
    assert levels["signal_density"] == INFO
    assert levels["cross_source_consistency"] == INFO
    assert levels["schema_regression"] == INFO
    assert levels["required_paths"] == INFO


# ---------------------------------------------------------------------------
# 7. Empty / degraded sources are tolerated
# ---------------------------------------------------------------------------


def test_strict_handles_empty_payloads_gracefully(tmp_path: Path) -> None:
    """All sources None → no crash; results carry sane verdicts."""
    payloads: dict[str, AdapterPayload | None] = {
        "super_pricing": None,
        "quant_trading": None,
        "index_research": None,
        "etf_512400": None,
    }
    results = vq.run_strict_checks(
        payloads,
        history_path=tmp_path / "fp.json",
        schema_dir=tmp_path,  # empty dir → schema_regression INFO (no baselines)
        signal_history_path=tmp_path / "signal_hist.json",
        today="2026-05-19",
    )
    names = {r.name for r in results}
    assert names == {
        "content_fingerprint_freshness",
        "signal_density",
        "cross_source_consistency",
        "schema_regression",
        "placeholder_detector",
        "temporal_coherence",
        "required_paths",
    }
    # Density: no rows anywhere → WARN; consistency: nothing to compare → INFO;
    # fingerprint: empty content → INFO ("skipped").
    levels = {r.name: r.level for r in results}
    assert levels["signal_density"] == WARN
    assert levels["cross_source_consistency"] == INFO
    assert levels["content_fingerprint_freshness"] == INFO
    # Placeholder detector against None payloads has nothing to scan → INFO.
    assert levels["placeholder_detector"] == INFO
    # Temporal coherence with no payloads has no time series → INFO (skipped).
    assert levels["temporal_coherence"] == INFO


# ---------------------------------------------------------------------------
# 8. CLI integration: --strict flag adds the quality checks
# ---------------------------------------------------------------------------


def test_fingerprint_normalization_tolerates_nonnumeric_impact() -> None:
    """Placeholder avg_impact values should hash as 0.0, not crash."""
    rows = vq._normalize_policy_records_for_fingerprint(
        _super_pricing_payload(
            signals=[{"industry": "电网", "avg_impact": "N/A", "signal": "neutral"}]
        )
    )
    assert rows == [{"industry": "电网", "signal": "neutral", "impact": 0.0}]


class _FakeResolution:
    def to_dict(self) -> dict[str, str]:
        return {"mode": "test", "available": "true"}


class _FakeAdapter:
    def __init__(self, payload: AdapterPayload | None) -> None:
        self._payload = payload

    def fetch(self) -> AdapterPayload | None:
        return self._payload


def test_cli_validate_strict_nonnumeric_policy_json_no_traceback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Strict validate should emit structured JSON for bad avg_impact strings."""
    monkeypatch.setattr(vq, "DEFAULT_FINGERPRINT_HISTORY", tmp_path / "fp.json")
    bad_super_pricing = _super_pricing_payload(
        signals=[{"industry": "电网", "avg_impact": "N/A", "signal": "neutral"}],
        metals=[{"metal": "copper", "name_cn": "铜", "price_change_pct": 1.0}],
    )
    monkeypatch.setattr(
        cli_mod,
        "build_default_adapters",
        lambda config: {"super_pricing": _FakeAdapter(bad_super_pricing)},
    )
    monkeypatch.setattr(
        cli_mod,
        "resolve_all_sources",
        lambda adapters: {"super_pricing": _FakeResolution()},
    )

    code = main(["validate", "--strict", "--json"])
    captured = capsys.readouterr()
    parsed = json.loads(captured.out)
    names = {check["name"] for check in parsed["checks"]}

    assert "content_fingerprint_freshness" in names
    assert code in (EXIT_OK, EXIT_WARN, EXIT_FAIL)
    assert "Traceback" not in captured.err


def test_strict_shell_guards_use_fail_on_warn() -> None:
    """Strict launch paths must not treat validator runtime failures as WARN-only."""
    root = vq._PROJECT_ROOT
    generate_daily = (root / "scripts" / "generate_daily.sh").read_text(encoding="utf-8")
    run_now = (root / "scripts" / "run_now.sh").read_text(encoding="utf-8")

    assert "--strict --fail-on-warn" in generate_daily
    assert "--strict --fail-on-warn" in run_now
    assert '"${strict_rc}" -ne 0' in run_now


# ---------------------------------------------------------------------------
# 8. CLI integration: --strict flag adds the quality checks
# ---------------------------------------------------------------------------


def test_cli_validate_strict_runs_new_checks(
    patched_default_paths: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`validate --strict` extends the result list with all quality checks."""
    # Redirect fingerprint + signal-history files to tmp so the test
    # doesn't write to output/.
    monkeypatch.setattr(vq, "DEFAULT_FINGERPRINT_HISTORY", tmp_path / "fp.json")
    monkeypatch.setattr(vq, "DEFAULT_SIGNAL_HISTORY", tmp_path / "signal_hist.json")
    code = main(["validate", "--strict", "--json"])
    out = capsys.readouterr().out
    parsed = json.loads(out)
    names = [c["name"] for c in parsed["checks"]]
    # 7 freshness/structural + 7 quality = 14
    assert len(parsed["checks"]) == 14
    for required in (
        "content_fingerprint_freshness",
        "signal_density",
        "cross_source_consistency",
        "schema_regression",
        "placeholder_detector",
        "temporal_coherence",
        "required_paths",
    ):
        assert required in names
    assert code in (EXIT_OK, EXIT_WARN, EXIT_FAIL)


def test_cli_validate_default_skips_quality_checks(
    patched_default_paths: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Plain `validate` with no flags must NOT include any v0.12 check (back-compat)."""
    code = main(["validate", "--json"])
    parsed = json.loads(capsys.readouterr().out)
    names = [c["name"] for c in parsed["checks"]]
    assert "content_fingerprint_freshness" not in names
    assert "signal_density" not in names
    assert "cross_source_consistency" not in names
    assert "schema_regression" not in names
    assert "placeholder_detector" not in names
    assert "temporal_coherence" not in names
    assert "required_paths" not in names
    assert len(parsed["checks"]) == 7
    assert code in (EXIT_OK, EXIT_WARN, EXIT_FAIL)


def test_cli_validate_single_check_flag(
    patched_default_paths: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`--check-density` alone adds only the density check."""
    monkeypatch.setattr(vq, "DEFAULT_FINGERPRINT_HISTORY", tmp_path / "fp.json")
    code = main(["validate", "--check-density", "--json"])
    parsed = json.loads(capsys.readouterr().out)
    names = [c["name"] for c in parsed["checks"]]
    assert "signal_density" in names
    assert "content_fingerprint_freshness" not in names
    assert "cross_source_consistency" not in names
    assert "schema_regression" not in names
    assert len(parsed["checks"]) == 8  # 7 baseline + 1
    assert code in (EXIT_OK, EXIT_WARN, EXIT_FAIL)


# ---------------------------------------------------------------------------
# 9. Placeholder detector (FAIL on shipped placeholder)
# ---------------------------------------------------------------------------


def test_placeholder_clean_payload_passes() -> None:
    """Production-shaped payload with no placeholder strings → INFO."""
    payloads = {
        "super_pricing": _super_pricing_payload(
            signals=[
                {"industry": "新能源汽车", "avg_impact": -0.4, "mentions": 50, "signal": "bearish"},
                {"industry": "电网", "avg_impact": 0.05, "mentions": 4, "signal": "neutral"},
            ],
            metals=[
                {"metal": "copper", "name_cn": "铜", "price_change_pct": 1.2},
            ],
        ),
        "quant_trading": _quant_payload(
            industries=[
                {
                    "industry": "新能源汽车",
                    "heat_score": 0.8,
                    "policy_signal": "bullish",
                    "policy_impact": 0.4,
                    "mentions": 3,
                }
            ]
        ),
    }
    r = vq.check_placeholder_detector(payloads)
    assert r.level == INFO
    assert "no placeholder" in r.message


def test_placeholder_cjk_test_in_industry_name_fails() -> None:
    """'测试' appearing in an industry name FAILs (the foundational bug)."""
    payloads = {
        "quant_trading": _quant_payload(
            industries=[
                {
                    "industry": "测试行业",
                    "heat_score": 0.5,
                    "policy_signal": "neutral",
                    "policy_impact": 0.0,
                    "mentions": 0,
                }
            ]
        )
    }
    r = vq.check_placeholder_detector(payloads)
    assert r.level == FAIL
    assert "测试" in r.message or "cjk_test" in r.message
    assert r.detail is not None
    hits = r.detail["hits_per_source"]["quant_trading"]
    assert any(h["pattern"] == "cjk_test" for h in hits)


def test_placeholder_todo_in_factor_description_fails() -> None:
    """A 'TODO' string anywhere in the payload FAILs."""
    payloads = {
        "super_pricing": _payload(
            "super_pricing",
            {
                "policy_radar": {
                    "industry_signals": [
                        {
                            "industry": "电网",
                            "signal": "neutral",
                            "factor_description": "TODO: refine impact weighting",
                            "avg_impact": 0.0,
                        }
                    ],
                    "policy_count": 1,
                },
                "macro_hf": {"metals": []},
            },
        )
    }
    r = vq.check_placeholder_detector(payloads)
    assert r.level == FAIL
    assert r.detail is not None
    hits = r.detail["hits_per_source"]["super_pricing"]
    patterns = {h["pattern"] for h in hits}
    assert "en_todo" in patterns


def test_placeholder_false_positive_guard_single_char_shi() -> None:
    """'试用期股' must NOT match — the single char '试' is not a placeholder."""
    payloads = {
        "quant_trading": _quant_payload(
            industries=[
                {
                    "industry": "试用期股",  # legitimate Chinese industry term
                    "heat_score": 0.5,
                    "policy_signal": "neutral",
                    "policy_impact": 0.0,
                    "mentions": 2,
                }
            ]
        )
    }
    r = vq.check_placeholder_detector(payloads)
    assert r.level == INFO, (
        f"single-char '试' must not be flagged; got {r.message}"
    )


def test_placeholder_xxx_scaffold_fails() -> None:
    """'XXX' scaffold marker FAILs (catches templating leakage)."""
    payloads = {
        "index_research": _payload(
            "index_research",
            {
                "verdicts": [
                    {"hid": "H1", "note": "Will refactor metric XXX later."}
                ]
            },
        )
    }
    r = vq.check_placeholder_detector(payloads)
    assert r.level == FAIL
    assert r.detail is not None
    hits = r.detail["hits_per_source"]["index_research"]
    assert any(h["pattern"] == "scaffold_xxx" for h in hits)


def test_placeholder_regression_archived_quant_payload(tmp_path: Path) -> None:
    """Loads the archived quant payload from when '测试行业' shipped → FAIL.

    Saved at ``tests/fixtures/regression/quant_summary_with_test_industry.json``.
    The check must detect the placeholder both inside ``providers.policy_radar``
    and ``providers.industry_heat`` rows.
    """
    fixture = (
        Path(__file__).parent
        / "fixtures"
        / "regression"
        / "quant_summary_with_test_industry.json"
    )
    raw = json.loads(fixture.read_text(encoding="utf-8"))
    payloads = {"quant_trading": _payload("quant_trading", raw)}
    r = vq.check_placeholder_detector(payloads)
    assert r.level == FAIL, "archived '测试行业' payload must FAIL the check"
    assert r.detail is not None
    hits = r.detail["hits_per_source"]["quant_trading"]
    # At least two hits — the policy_radar entry AND the industry_heat entry.
    test_hits = [h for h in hits if h["pattern"] == "cjk_test"]
    assert len(test_hits) >= 2, (
        f"expected ≥2 '测试' hits across policy_radar + industry_heat; got {hits}"
    )
    paths = {h["path"] for h in test_hits}
    assert any("policy_radar" in p for p in paths)
    assert any("industry_heat" in p for p in paths)


def test_placeholder_allowlist_paper_trading_smoke_profile() -> None:
    """`paper_trading.active_profiles` carries internal test names — INFO, not FAIL."""
    payloads = {
        "quant_trading": _payload(
            "quant_trading",
            {
                "industries": [],
                "paper_trading": {
                    "active_profiles": ["e2e-smoke", "test_env"],
                    "available": True,
                },
            },
        )
    }
    r = vq.check_placeholder_detector(payloads)
    assert r.level == INFO, (
        f"allowlisted path should suppress placeholder hit; got {r.message}"
    )


def test_placeholder_cli_strict_fails_on_test_industry(
    patched_default_paths: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`validate --strict` with a polluted payload exits non-zero (FAIL)."""
    monkeypatch.setattr(vq, "DEFAULT_FINGERPRINT_HISTORY", tmp_path / "fp.json")
    monkeypatch.setattr(vq, "DEFAULT_SIGNAL_HISTORY", tmp_path / "signal_hist.json")

    bad_quant = _quant_payload(
        industries=[
            {
                "industry": "测试行业",
                "heat_score": 0.5,
                "policy_signal": "neutral",
                "policy_impact": 0.0,
                "mentions": 0,
            }
        ]
    )
    monkeypatch.setattr(
        cli_mod,
        "build_default_adapters",
        lambda config: {"quant_trading": _FakeAdapter(bad_quant)},
    )
    monkeypatch.setattr(
        cli_mod,
        "resolve_all_sources",
        lambda adapters: {"quant_trading": _FakeResolution()},
    )

    code = main(["validate", "--strict", "--json"])
    parsed = json.loads(capsys.readouterr().out)
    placeholder = next(c for c in parsed["checks"] if c["name"] == "placeholder_detector")
    assert placeholder["level"] == FAIL
    # Strict + placeholder hit → process exit non-zero. Without --fail-on-warn,
    # any FAIL escalates to EXIT_FAIL.
    assert code == EXIT_FAIL


# ---------------------------------------------------------------------------
# 10. Temporal coherence (WARN on day-over-day jitter)
# ---------------------------------------------------------------------------


def test_temporal_stable_signal_passes(tmp_path: Path) -> None:
    """7 days of the same signal → flip rate 0 → INFO."""
    history_path = tmp_path / "signal_hist.json"
    days = [f"2026-05-{12 + i:02d}" for i in range(7)]
    for day in days:
        payloads = {
            "super_pricing": _super_pricing_payload(
                signals={
                    "新能源汽车": {
                        "avg_impact": -0.4,
                        "mentions": 50,
                        "signal": "bearish",
                    }
                }
            )
        }
        r = vq.check_temporal_coherence(
            payloads, history_path=history_path, today=day
        )
    # Final-day verdict — stable bearish across 7 days.
    assert r.level == INFO
    assert r.detail is not None
    sp_signals = r.detail["per_source"]["super_pricing"]["signals"]
    flip_rates = [s["flip_rate"] for s in sp_signals.values()]
    assert all(fr == 0.0 for fr in flip_rates)


def test_temporal_jittery_signal_warns(tmp_path: Path) -> None:
    """Alternating bullish/bearish for 6 days → 100% flip rate → WARN."""
    history_path = tmp_path / "signal_hist.json"
    rotation = ["bullish", "bearish", "bullish", "bearish", "bullish", "bearish"]
    days = [f"2026-05-{12 + i:02d}" for i in range(6)]
    for day, sig in zip(days, rotation, strict=True):
        payloads = {
            "super_pricing": _super_pricing_payload(
                signals={
                    "新能源汽车": {
                        "avg_impact": -0.4 if sig == "bearish" else 0.4,
                        "mentions": 50,
                        "signal": sig,
                    }
                }
            )
        }
        r = vq.check_temporal_coherence(
            payloads, history_path=history_path, today=day
        )
    assert r.level == WARN
    assert "新能源汽车" in r.message
    assert r.detail is not None
    jittery = r.detail["jittery"]
    assert len(jittery) == 1
    assert jittery[0]["flip_rate"] >= 0.9


def test_temporal_jittery_with_regime_change_passes(tmp_path: Path) -> None:
    """Same jitter pattern + ``regime_change_event=True`` → INFO (legitimized)."""
    history_path = tmp_path / "signal_hist.json"
    rotation = ["bullish", "bearish", "bullish", "bearish", "bullish", "bearish"]
    days = [f"2026-05-{12 + i:02d}" for i in range(6)]
    for day, sig in zip(days, rotation, strict=True):
        # Build a super_pricing payload that ALSO carries a regime-change flag
        # — operators set this when a real volatility event drives the flips.
        payload_data = {
            "policy_radar": {
                "industry_signals": {
                    "新能源汽车": {
                        "avg_impact": -0.4 if sig == "bearish" else 0.4,
                        "mentions": 50,
                        "signal": sig,
                    }
                },
                "policy_count": 1,
            },
            "macro_hf": {"metals": []},
            "regime_change_event": True,
        }
        payloads = {"super_pricing": _payload("super_pricing", payload_data)}
        r = vq.check_temporal_coherence(
            payloads, history_path=history_path, today=day
        )
    assert r.level == INFO, (
        "regime_change_event=True should suppress WARN on a legitimate flip"
    )
    assert r.detail is not None
    assert r.detail["jittery"] == []
    assert r.detail["per_source"]["super_pricing"]["regime_change_event"] is True


def test_temporal_missing_time_series_skipped(tmp_path: Path) -> None:
    """No signals to score across all sources → INFO ("skipped")."""
    history_path = tmp_path / "signal_hist.json"
    payloads: dict[str, AdapterPayload | None] = {
        "super_pricing": None,
        "quant_trading": None,
        "index_research": None,
        "etf_512400": None,
    }
    r = vq.check_temporal_coherence(
        payloads, history_path=history_path, today="2026-05-19"
    )
    assert r.level == INFO
    assert "skipped" in r.message.lower()
    # History file should NOT have been written when there's nothing to record.
    assert not history_path.exists()


def test_temporal_history_persistence_and_trimming(tmp_path: Path) -> None:
    """Rolling window caps at TEMPORAL_HISTORY_DAYS; older entries get dropped."""
    history_path = tmp_path / "signal_hist.json"
    # 10 days of stable bullish → series gets trimmed to TEMPORAL_HISTORY_DAYS=7.
    days = [f"2026-05-{10 + i:02d}" for i in range(10)]
    for day in days:
        payloads = {
            "super_pricing": _super_pricing_payload(
                signals={
                    "AI算力": {
                        "avg_impact": 0.3,
                        "mentions": 5,
                        "signal": "bullish",
                    }
                }
            )
        }
        vq.check_temporal_coherence(
            payloads, history_path=history_path, today=day
        )
    loaded = vq.load_signal_history(history_path)
    series = loaded["super_pricing"]["policy_radar.AI算力"]
    assert len(series) == vq.TEMPORAL_HISTORY_DAYS
    # The most recent observation date must be retained.
    assert series[-1].date == days[-1]


# ---------------------------------------------------------------------------
# 11. Per-check CLI flags for the new checks
# ---------------------------------------------------------------------------


def test_cli_check_placeholder_flag(
    patched_default_paths: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--check-placeholder`` runs only the placeholder detector."""
    code = main(["validate", "--check-placeholder", "--json"])
    parsed = json.loads(capsys.readouterr().out)
    names = [c["name"] for c in parsed["checks"]]
    assert "placeholder_detector" in names
    assert "temporal_coherence" not in names
    assert "signal_density" not in names
    assert len(parsed["checks"]) == 8  # 7 baseline + 1
    assert code in (EXIT_OK, EXIT_WARN, EXIT_FAIL)


def test_cli_check_temporal_flag(
    patched_default_paths: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--check-temporal`` runs only the temporal coherence check."""
    monkeypatch.setattr(vq, "DEFAULT_SIGNAL_HISTORY", tmp_path / "signal_hist.json")
    code = main(["validate", "--check-temporal", "--json"])
    parsed = json.loads(capsys.readouterr().out)
    names = [c["name"] for c in parsed["checks"]]
    assert "temporal_coherence" in names
    assert "placeholder_detector" not in names
    assert "signal_density" not in names
    assert len(parsed["checks"]) == 8  # 7 baseline + 1
    assert code in (EXIT_OK, EXIT_WARN, EXIT_FAIL)


def test_cli_check_required_paths_flag(
    patched_default_paths: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--check-required-paths`` runs only the raw upstream path audit."""
    code = main(["validate", "--check-required-paths", "--json"])
    parsed = json.loads(capsys.readouterr().out)
    names = [c["name"] for c in parsed["checks"]]
    assert "required_paths" in names
    assert "temporal_coherence" not in names
    assert "schema_regression" not in names
    assert len(parsed["checks"]) == 8  # 7 baseline + 1
    assert code in (EXIT_OK, EXIT_WARN, EXIT_FAIL)
