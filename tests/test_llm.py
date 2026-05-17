"""Tests for the optional LLM rephrase guardrails and usage log."""

from __future__ import annotations

from pathlib import Path

from cn_altdata_brief.llm import RephraseResult, validate_rephrase
from cn_altdata_brief.llm.usage import aggregate_usage, log_usage


def test_validate_rephrase_rejects_missing_numbers_and_industries() -> None:
    raw = "今日核心信号是 **新能源汽车** 的 avg_impact=-0.388。"

    ok, reason = validate_rephrase(raw, "今日核心信号来自新能源车。", ["新能源汽车"])

    assert ok is False
    assert reason is not None
    assert "numbers missing" in reason


def test_validate_rephrase_accepts_fact_preserving_polish() -> None:
    raw = "今日核心信号是 **新能源汽车** 的 avg_impact=-0.388。"
    polished = "今日最需要留意的是新能源汽车，avg_impact=-0.388。"

    ok, reason = validate_rephrase(raw, polished, ["新能源汽车"])

    assert ok is True
    assert reason is None


def test_log_usage_and_aggregate(tmp_path: Path) -> None:
    log_path = tmp_path / "llm_usage.jsonl"
    log_usage(
        RephraseResult(
            raw_text="raw",
            polished_text="polished",
            status="ok",
            llm_model_used="fake-model",
            latency_ms=10,
            input_tokens=100,
            output_tokens=50,
            prompt_hash="abcdef123456",
        ),
        log_path,
        extra={"date": "2026-05-17"},
    )
    log_usage(
        RephraseResult(
            raw_text="raw",
            polished_text="raw",
            status="validation_failed",
            llm_model_used="fake-model",
            latency_ms=20,
            input_tokens=20,
            output_tokens=10,
        ),
        log_path,
    )

    usage = aggregate_usage(log_path)

    assert usage.total_calls == 2
    assert usage.ok_calls == 1
    assert usage.fallback_calls == 1
    assert usage.per_status == {"ok": 1, "validation_failed": 1}
    assert usage.per_model == {"fake-model": 2}
    assert usage.input_tokens == 120
    assert usage.output_tokens == 60
