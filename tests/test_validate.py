"""Tests for the ``validate`` subcommand and its check predicates."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from altdata_brief import validate as validate_mod
from altdata_brief.adapters import (
    ETF512400Adapter,
    IndexResearchAdapter,
    QuantTradingAdapter,
    SuperPricingAdapter,
)
from altdata_brief.adapters.base import AdapterPayload
from altdata_brief.cli import main
from altdata_brief.validate import (
    EXIT_FAIL,
    EXIT_OK,
    EXIT_WARN,
    FAIL,
    INFO,
    WARN,
    CheckResult,
    run_all_checks,
    summarize,
)

# -- helpers ------------------------------------------------------------


@pytest.fixture
def all_payloads(
    super_pricing_cache: Path,
    quant_trading_cache: Path,
    index_research_tables: Path,
    etf_512400_snapshot: Path,
) -> dict[str, AdapterPayload | None]:
    return {
        "super_pricing": SuperPricingAdapter(cache_dir=super_pricing_cache).fetch(),
        "quant_trading": QuantTradingAdapter(cache_dir=quant_trading_cache).fetch(),
        "index_research": IndexResearchAdapter(
            table_dir=index_research_tables, figure_dir=index_research_tables
        ).fetch(),
        "etf_512400": ETF512400Adapter(snapshot_path=etf_512400_snapshot).fetch(),
    }


# patched_default_paths fixture lives in conftest.py and is shared.

# -- individual check predicates ---------------------------------------


def test_all_fixture_checks_pass_or_warn(all_payloads) -> None:
    assert validate_mod._check_policy_industries(all_payloads["super_pricing"]).level == INFO
    assert validate_mod._check_macro_metals(all_payloads["super_pricing"]).level == INFO
    assert validate_mod._check_verdict_completeness(all_payloads["index_research"]).level == INFO


def test_missing_payloads_fail() -> None:
    assert validate_mod._check_policy_industries(None).level == FAIL
    assert validate_mod._check_macro_metals(None).level == FAIL
    assert validate_mod._check_super_pricing_provider_freshness(None).level == FAIL
    assert validate_mod._check_etf_snapshot_age(None).level == FAIL
    assert validate_mod._check_etf_required_source_health(None).level == FAIL
    assert validate_mod._check_verdict_completeness(None).level == FAIL


def _payload(data: dict) -> AdapterPayload:
    return AdapterPayload(source="x", fetched_at="t", cache_path=None, live=False, data=data)


def test_synthetic_failure_modes() -> None:
    too_few_industries = _payload(
        {"policy_radar": {"industry_signals": [
            {"industry": "A", "mentions": 5, "avg_impact": 0.1, "signal": "x"},
            {"industry": "B", "mentions": 0, "avg_impact": 0.0, "signal": "x"},
        ]}}
    )
    assert validate_mod._check_policy_industries(too_few_industries).level == FAIL

    nan_metals = _payload(
        {"macro_hf": {"metals": [
            {"name_cn": "铜", "price_change_pct": float("nan")},
            {"name_cn": "铝", "price_change_pct": None},
        ]}}
    )
    assert validate_mod._check_macro_metals(nan_metals).level == FAIL

    too_few_verdicts = _payload({"verdicts": [{"hid": "H1"}, {"hid": "H2"}]})
    assert validate_mod._check_verdict_completeness(too_few_verdicts).level == FAIL


def test_etf_snapshot_age_buckets() -> None:
    def _etf(trade_date: str | None) -> AdapterPayload:
        return _payload({"trade_date": trade_date, "nav": {}, "generated_at": None})

    today = datetime.now(UTC).date()
    assert validate_mod._check_etf_snapshot_age(_etf(today.isoformat())).level == INFO
    assert validate_mod._check_etf_snapshot_age(_etf((today - timedelta(days=10)).isoformat())).level == WARN
    assert validate_mod._check_etf_snapshot_age(_etf("not-a-date")).level == WARN


def test_provider_freshness_warns_on_stale_inner_timestamps() -> None:
    stale = (datetime.now(UTC) - timedelta(hours=48)).replace(microsecond=0).isoformat()
    payload = _payload(
        {
            "policy_radar": {"timestamp": stale},
            "macro_hf": {"timestamp": stale},
        }
    )
    result = validate_mod._check_super_pricing_provider_freshness(payload)
    assert result.level == WARN
    assert "stale" in result.message


def test_provider_freshness_stale_message_includes_age_and_timestamp() -> None:
    """Daily Actions logs should expose stale provider timestamp detail without failing."""
    stale = (datetime.now(UTC) - timedelta(hours=48)).replace(microsecond=0).isoformat()
    payload = _payload(
        {
            "policy_radar": {"timestamp": stale},
            "macro_hf": {"timestamp": stale},
        }
    )

    result = validate_mod._check_super_pricing_provider_freshness(payload)

    assert result.level == WARN
    assert "policy_radar=" in result.message
    assert "macro_hf=" in result.message
    assert "age=" in result.message
    assert stale in result.message


def test_provider_freshness_accepts_recent_inner_timestamps() -> None:
    fresh = datetime.now(UTC).replace(microsecond=0).isoformat()
    payload = _payload(
        {
            "policy_radar": {"timestamp": fresh},
            "macro_hf": {"timestamp": fresh},
        }
    )
    assert validate_mod._check_super_pricing_provider_freshness(payload).level == INFO


def test_etf_required_source_health_blocks_stale_quote() -> None:
    today = datetime.now(UTC).date()
    payload = _payload(
        {
            "trade_date": (today - timedelta(days=12)).isoformat(),
            "source_health": {
                "required_total": 4,
                "required_ok": 3,
                "quote_ok": False,
                "quote_fallback": True,
                "required_degraded": [{"id": "quote", "fallback": True}],
            },
        }
    )
    result = validate_mod._check_etf_required_source_health(payload)
    assert result.level == FAIL
    assert "quote source degraded" in result.message


def test_etf_required_source_health_stale_quote_message_includes_dates() -> None:
    """Daily Actions logs should identify stale ETF quote dates without blocking."""
    today = datetime.now(UTC).date()
    stale_trade_date = (today - timedelta(days=6)).isoformat()
    payload = _payload(
        {
            "trade_date": stale_trade_date,
            "source_health": {
                "required_total": 4,
                "required_ok": 4,
                "quote_ok": True,
                "quote_fallback": False,
            },
        }
    )

    result = validate_mod._check_etf_required_source_health(payload)

    assert result.level == WARN
    assert f"trade_date={stale_trade_date}" in result.message
    assert f"today_utc={today.isoformat()}" in result.message


def test_etf_required_source_health_accepts_fresh_quote() -> None:
    today = datetime.now(UTC).date()
    payload = _payload(
        {
            "trade_date": today.isoformat(),
            "source_health": {
                "required_total": 4,
                "required_ok": 4,
                "quote_ok": True,
                "quote_fallback": False,
            },
        }
    )
    assert validate_mod._check_etf_required_source_health(payload).level == INFO


# -- summarize ---------------------------------------------------------


def test_summarize_exit_codes() -> None:
    assert summarize([CheckResult("a", INFO, "ok")]) == EXIT_OK
    assert summarize([CheckResult("a", WARN, "meh")]) == EXIT_WARN
    assert summarize([CheckResult("a", WARN, "meh")], fail_on_warn=True) == EXIT_FAIL
    assert summarize([CheckResult("a", FAIL, "bad"), CheckResult("b", WARN, "meh")]) == EXIT_FAIL


# -- run_all_checks + CLI integration ----------------------------------


def test_run_all_checks_against_fixtures(all_payloads, tmp_path: Path) -> None:
    # Inject empty paths so the new public_summary_freshness check does not
    # accidentally pass-or-fail based on the maintainer's laptop state.
    results = run_all_checks(
        all_payloads,
        public_summary_paths={
            "super_pricing": tmp_path / "missing_sp.json",
            "index_research": tmp_path / "missing_ix.json",
        },
    )
    assert len(results) == 7
    assert all(r.level in (INFO, WARN, FAIL) for r in results)
    names = [r.name for r in results]
    assert "public_summary_freshness" in names
    assert "super_pricing.provider_freshness" in names
    assert "etf_512400.required_source_health" in names


def test_cli_validate_human_output(
    patched_default_paths: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(validate_mod, "PROVIDER_FRESH_HOURS", 999999)
    monkeypatch.setattr(validate_mod, "MAX_ETF_QUOTE_AGE_DAYS", 999999)
    monkeypatch.setattr(validate_mod, "MAX_ETF_SNAPSHOT_AGE_DAYS", 999999)
    code = main(["validate"])
    out = capsys.readouterr().out
    assert "policy_radar.industries_with_mentions" in out
    assert "macro_hf.metals_with_weekly_change" in out
    assert "super_pricing.provider_freshness" in out
    assert "etf_512400.snapshot_age" in out
    assert "etf_512400.required_source_health" in out
    assert "index_research.verdict_completeness" in out
    assert "public_summary_freshness" in out
    assert code in (EXIT_OK, EXIT_WARN)


def test_cli_validate_json_output(
    patched_default_paths: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(validate_mod, "PROVIDER_FRESH_HOURS", 999999)
    monkeypatch.setattr(validate_mod, "MAX_ETF_QUOTE_AGE_DAYS", 999999)
    monkeypatch.setattr(validate_mod, "MAX_ETF_SNAPSHOT_AGE_DAYS", 999999)
    code = main(["validate", "--json"])
    parsed = json.loads(capsys.readouterr().out)
    assert len(parsed["checks"]) == 7
    assert parsed["exit_code"] == code


def test_cli_validate_fail_on_warn_escalates(patched_default_paths: None) -> None:
    code_plain = main(["validate"])
    code_strict = main(["validate", "--fail-on-warn"])
    if code_plain == EXIT_WARN:
        assert code_strict == EXIT_FAIL
    else:
        assert code_strict in (EXIT_OK, EXIT_FAIL)


# -- public_summary_freshness check ------------------------------------


def _write_summary(path: Path, generated_at: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"generated_at": generated_at, "schema_version": 1}),
        encoding="utf-8",
    )


def test_public_summary_freshness_all_fresh(tmp_path: Path) -> None:
    sp_path = tmp_path / "sp" / "alt_data_summary.json"
    ix_path = tmp_path / "ix" / "index_research_summary.json"
    now_iso = datetime.now(UTC).replace(microsecond=0).isoformat()
    _write_summary(sp_path, now_iso)
    _write_summary(ix_path, now_iso)
    res = validate_mod._check_public_summary_freshness(
        {"super_pricing": sp_path, "index_research": ix_path}
    )
    assert res.level == INFO
    assert "fresh" in res.message
    assert res.detail is not None
    assert res.detail["sources"]["super_pricing"]["present"] is True


def test_public_summary_freshness_missing_warns(tmp_path: Path) -> None:
    res = validate_mod._check_public_summary_freshness(
        {
            "super_pricing": tmp_path / "no" / "sp.json",
            "index_research": tmp_path / "no" / "ix.json",
        }
    )
    assert res.level == WARN
    assert "missing" in res.message


def test_public_summary_freshness_stale_warns(tmp_path: Path) -> None:
    sp_path = tmp_path / "sp" / "alt_data_summary.json"
    ix_path = tmp_path / "ix" / "index_research_summary.json"
    # 48h-old timestamps
    stale = (datetime.now(UTC) - timedelta(hours=48)).replace(microsecond=0).isoformat()
    _write_summary(sp_path, stale)
    _write_summary(ix_path, stale)
    res = validate_mod._check_public_summary_freshness(
        {"super_pricing": sp_path, "index_research": ix_path}
    )
    assert res.level == WARN
    assert "stale" in res.message


def test_public_summary_freshness_unparsable_generated_at(tmp_path: Path) -> None:
    sp_path = tmp_path / "sp.json"
    sp_path.write_text(json.dumps({"generated_at": "not a date"}), encoding="utf-8")
    # The index one we just make missing.
    res = validate_mod._check_public_summary_freshness(
        {"super_pricing": sp_path, "index_research": tmp_path / "missing.json"}
    )
    assert res.level == WARN
