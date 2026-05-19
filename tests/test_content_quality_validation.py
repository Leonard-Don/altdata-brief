"""Tests for the v0.12 content-quality validate checks.

Covers the four new check functions in ``validate_quality.py`` plus
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
# 5. All-pass case across all four checks
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
    # fingerprint, density, consistency all INFO; schema INFO (only super_pricing baseline loaded).
    assert levels["content_fingerprint_freshness"] == INFO
    assert levels["signal_density"] == INFO
    assert levels["cross_source_consistency"] == INFO
    assert levels["schema_regression"] == INFO


# ---------------------------------------------------------------------------
# 6. Empty / degraded sources are tolerated
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
        today="2026-05-19",
    )
    names = {r.name for r in results}
    assert names == {
        "content_fingerprint_freshness",
        "signal_density",
        "cross_source_consistency",
        "schema_regression",
    }
    # Density: no rows anywhere → WARN; consistency: nothing to compare → INFO;
    # fingerprint: empty content → INFO ("skipped").
    levels = {r.name: r.level for r in results}
    assert levels["signal_density"] == WARN
    assert levels["cross_source_consistency"] == INFO
    assert levels["content_fingerprint_freshness"] == INFO


# ---------------------------------------------------------------------------
# 7. CLI integration: --strict flag adds the four checks
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
# 7. CLI integration: --strict flag adds the four checks
# ---------------------------------------------------------------------------


def test_cli_validate_strict_runs_new_checks(
    patched_default_paths: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`validate --strict` extends the result list with the four quality checks."""
    # Redirect fingerprint history to tmp so the test doesn't write to output/.
    monkeypatch.setattr(vq, "DEFAULT_FINGERPRINT_HISTORY", tmp_path / "fp.json")
    code = main(["validate", "--strict", "--json"])
    out = capsys.readouterr().out
    parsed = json.loads(out)
    names = [c["name"] for c in parsed["checks"]]
    # 5 freshness/structural + 4 quality = 9
    assert len(parsed["checks"]) == 9
    for required in (
        "content_fingerprint_freshness",
        "signal_density",
        "cross_source_consistency",
        "schema_regression",
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
    assert len(parsed["checks"]) == 5
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
    assert len(parsed["checks"]) == 6  # 5 baseline + 1
    assert code in (EXIT_OK, EXIT_WARN, EXIT_FAIL)
