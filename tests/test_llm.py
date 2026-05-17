"""Tests for the optional LLM rephrase guardrails and usage log."""

from __future__ import annotations

import json
from dataclasses import asdict
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


def test_log_usage_drops_raw_and_polished_extra_fields(tmp_path: Path) -> None:
    log_path = tmp_path / "llm_usage.jsonl"

    log_usage(
        RephraseResult(
            raw_text="raw private phrase",
            polished_text="polished private phrase",
            status="sdk_missing",
        ),
        log_path,
        extra={
            "date": "2026-05-17",
            "section": "observation",
            "raw_text": "must not be written",
            "polished_text": "must not be written",
            "source_text": "alternate raw private phrase must not be written",
            "note": "polished private phrase must not be written",
        },
    )

    record = json.loads(log_path.read_text(encoding="utf-8"))
    assert record["status"] == "sdk_missing"
    assert record["date"] == "2026-05-17"
    assert record["section"] == "observation"
    assert "raw_text" not in record
    assert "polished_text" not in record
    assert "source_text" not in record
    assert "note" not in record


def test_aggregate_usage_tolerates_malformed_partial_records_without_text_leak(
    tmp_path: Path,
) -> None:
    log_path = tmp_path / "llm_usage.jsonl"
    log_path.write_text(
        "\n".join(
            [
                "{not json",
                "[]",
                json.dumps(
                    {
                        "timestamp": "2026-05-17T00:00:00Z",
                        "model": "safe-model",
                        "status": "ok",
                        "input_tokens": "12",
                        "output_tokens": "bad-count",
                        "latency_ms": "15.5",
                        "est_cost_usd": "bad-cost",
                        "raw_text": "raw private phrase must stay private",
                        "polished_text": "polished private phrase must stay private",
                    }
                ),
                json.dumps(
                    {
                        "timestamp": "2026-05-17T00:01:00Z",
                        "status": "sdk_missing",
                    }
                ),
                json.dumps(
                    {
                        "timestamp": 123,
                        "model": {"raw_text": "model private phrase"},
                        "status": {"polished_text": "status private phrase"},
                        "input_tokens": {"bad": "shape"},
                        "output_tokens": -8,
                    }
                ),
                json.dumps(
                    {
                        "timestamp": "2026-05-17T00:02:00Z",
                        "model": "今日核心信号是 private prose",
                        "status": "polished private phrase as status",
                    }
                ),
            ]
        ),
        encoding="utf-8",
    )

    usage = aggregate_usage(log_path)
    rendered = json.dumps(asdict(usage), ensure_ascii=False)

    assert usage.total_calls == 4
    assert usage.ok_calls == 1
    assert usage.fallback_calls == 3
    assert usage.input_tokens == 12
    assert usage.output_tokens == 0
    assert usage.est_cost_usd == 0.0
    assert usage.avg_latency_ms == 15.5
    assert usage.per_status == {"ok": 1, "sdk_missing": 1, "unknown": 2}
    assert usage.per_model == {"safe-model": 1, "(none)": 2, "(redacted)": 1}
    assert "raw private phrase" not in rendered
    assert "polished private phrase" not in rendered
    assert "model private phrase" not in rendered
    assert "status private phrase" not in rendered
    assert "今日核心信号" not in rendered
    assert "private prose" not in rendered
