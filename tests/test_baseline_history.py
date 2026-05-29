"""v0.3: real 7-day baseline wired from super-pricing's narrative archive.

The v0.2 observation section anchored its "对比近 7 日" context lines on
hand-tuned module constants. v0.3 reads super-pricing's
``cache/alt_data/narrative_history.jsonl`` so the policy-impact baseline is
the *actual* rolling mean of recent snapshots, falling back to the constant
when the archive is absent (CI, fresh clones) or carries no usable signal.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from altdata_brief.adapters.base import AdapterPayload
from altdata_brief.synthesis import synthesize_observation
from altdata_brief.synthesis.baseline import (
    BASELINE_POLICY_IMPACT_7D,
    load_recent_history,
    resolve_baseline,
)


def _write_jsonl(path: Path, entries: list[dict]) -> None:
    path.write_text(
        "\n".join(json.dumps(e, ensure_ascii=False) for e in entries) + "\n",
        encoding="utf-8",
    )


def _narrative_entry(avg_impact: float | None, industry: str | None = "新能源汽车") -> dict:
    """Mirror the real narrative_history.jsonl shape (avg_impact lives in prose)."""
    if avg_impact is None:
        summary = "宏观高频库存信号（相关金属）：SHFE 铜 destocking (库存去化)。"
    else:
        summary = (
            f"政策雷达本周捕获 9 条相关政策记录，{industry} 行业影响力 "
            f"avg_impact={avg_impact}, 偏空。"
        )
    return {
        "archived_at": "2026-05-20T00:00:00+00:00",
        "industry": industry,
        "summary": summary,
        "bullets": [summary],
    }


# -- load_recent_history ---------------------------------------------------


def test_load_recent_history_extracts_abs_policy_impact_series(tmp_path: Path) -> None:
    p = tmp_path / "narrative_history.jsonl"
    _write_jsonl(p, [_narrative_entry(0.10), _narrative_entry(-0.30), _narrative_entry(0.20)])
    hist = load_recent_history(p)
    assert hist is not None
    # absolute values (baseline is "average of |avg_impact|"), original order preserved
    assert hist["policy_impact_7d"] == [0.10, 0.30, 0.20]


def test_load_recent_history_filters_entries_without_avg_impact(tmp_path: Path) -> None:
    p = tmp_path / "narrative_history.jsonl"
    _write_jsonl(
        p,
        [_narrative_entry(0.10), _narrative_entry(None, industry=None), _narrative_entry(0.40)],
    )
    hist = load_recent_history(p)
    assert hist is not None
    assert hist["policy_impact_7d"] == [0.10, 0.40]


def test_load_recent_history_keeps_only_last_limit_signal_entries(tmp_path: Path) -> None:
    p = tmp_path / "narrative_history.jsonl"
    _write_jsonl(p, [_narrative_entry(round(i / 100, 2)) for i in range(1, 11)])  # 0.01..0.10
    hist = load_recent_history(p, limit=7)
    assert hist is not None
    assert len(hist["policy_impact_7d"]) == 7
    assert hist["policy_impact_7d"][0] == pytest.approx(0.04)
    assert hist["policy_impact_7d"][-1] == pytest.approx(0.10)


def test_load_recent_history_missing_file_returns_none(tmp_path: Path) -> None:
    assert load_recent_history(tmp_path / "does_not_exist.jsonl") is None


def test_load_recent_history_no_parseable_entries_returns_none(tmp_path: Path) -> None:
    p = tmp_path / "narrative_history.jsonl"
    _write_jsonl(p, [_narrative_entry(None, industry=None)])
    assert load_recent_history(p) is None


def test_load_recent_history_skips_malformed_lines(tmp_path: Path) -> None:
    p = tmp_path / "narrative_history.jsonl"
    p.write_text(
        json.dumps(_narrative_entry(0.10), ensure_ascii=False) + "\n"
        + "{ this is not valid json\n"
        + json.dumps(_narrative_entry(0.30), ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    hist = load_recent_history(p)
    assert hist is not None
    assert hist["policy_impact_7d"] == [0.10, 0.30]


# -- resolve_baseline ------------------------------------------------------


def test_resolve_baseline_returns_mean_when_present() -> None:
    hist = {"policy_impact_7d": [0.30, 0.40]}
    assert resolve_baseline(hist, "policy_impact_7d", BASELINE_POLICY_IMPACT_7D) == pytest.approx(0.35)


def test_resolve_baseline_falls_back_when_history_none() -> None:
    assert resolve_baseline(None, "policy_impact_7d", 0.18) == 0.18


def test_resolve_baseline_falls_back_when_key_missing_or_empty() -> None:
    assert resolve_baseline({}, "policy_impact_7d", 0.18) == 0.18
    assert resolve_baseline({"policy_impact_7d": []}, "policy_impact_7d", 0.18) == 0.18


# -- observation integration ----------------------------------------------


def _policy_payload(avg_impact: float = 0.40) -> AdapterPayload:
    """Minimal payload where the policy candidate is unambiguously the lead."""
    return AdapterPayload(
        source="super-pricing-system",
        fetched_at="2026-05-29T00:00:00+00:00",
        cache_path=None,
        live=False,
        data={
            "policy_radar": {
                "industry_signals": [
                    {"industry": "新能源汽车", "avg_impact": avg_impact, "mentions": 9}
                ],
                "policy_count": 9,
            }
        },
    )


def test_observation_uses_real_baseline_when_history_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "altdata_brief.synthesis.observation.load_recent_history",
        lambda *a, **k: {"policy_impact_7d": [0.30, 0.40]},
    )
    result = synthesize_observation(_policy_payload(0.40), None, None, None)
    context = result["sentences"][1]
    assert "0.35" in context  # real rolling mean of [0.30, 0.40]
    assert "0.18" not in context  # NOT the v0.2 hand-tuned constant


def test_observation_falls_back_to_constant_when_history_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "altdata_brief.synthesis.observation.load_recent_history",
        lambda *a, **k: None,
    )
    result = synthesize_observation(_policy_payload(0.40), None, None, None)
    context = result["sentences"][1]
    assert f"{BASELINE_POLICY_IMPACT_7D:.2f}" in context  # 0.18 fallback
