"""Tests for the optional LLM rephrase guardrails and usage log."""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

import pytest

from altdata_brief.llm import (
    RephraseResult,
    rephrase_observation,
    validate_rephrase,
)
from altdata_brief.llm import (
    anthropic_client as ac_mod,
)
from altdata_brief.llm.usage import aggregate_usage, log_usage


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


def test_aggregate_usage_days_window_skips_unparseable_timestamps(
    tmp_path: Path,
) -> None:
    log_path = tmp_path / "llm_usage.jsonl"
    log_path.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "timestamp": "2026-05-21T10:30:00Z",
                        "model": "recent-model",
                        "status": "ok",
                        "input_tokens": 10,
                    }
                ),
                json.dumps(
                    {
                        "timestamp": "2026-05-18T10:30:00Z",
                        "model": "old-model",
                        "status": "ok",
                        "input_tokens": 20,
                    }
                ),
                json.dumps(
                    {
                        "timestamp": "not-a-date",
                        "model": "malformed-ts-model",
                        "status": "ok",
                        "input_tokens": 30,
                    }
                ),
                json.dumps(
                    {
                        "model": "missing-ts-model",
                        "status": "ok",
                        "input_tokens": 40,
                    }
                ),
            ]
        ),
        encoding="utf-8",
    )

    usage = aggregate_usage(
        log_path,
        days=1,
        now=datetime(2026, 5, 21, 12, 0, tzinfo=UTC),
    )

    assert usage.total_calls == 1
    assert usage.input_tokens == 10
    assert usage.per_model == {"recent-model": 1}
    assert usage.first_ts == "2026-05-21T10:30:00Z"
    assert usage.last_ts == "2026-05-21T10:30:00Z"


# ---------------------------------------------------------------------------
# Fake-SDK harness for the rephrase_observation live path
#
# These tests never hit the real Anthropic API: they monkey-patch
# anthropic_client._sdk_module (the same shim production code probes) to
# inject a fake SDK, and set a dummy ANTHROPIC_API_KEY.
# ---------------------------------------------------------------------------


RAW_OBSERVATION = (
    "今日核心信号是 **新能源汽车** 政策转弱，avg_impact=-0.388。\n"
    "近 7 日该信号与 ETF 资金面方向一致。\n"
    "若该信号延续，需留意 avg_impact=-0.388 的方向。"
)


class _FakeMessage:
    """Minimal stand-in for an Anthropic ``Message`` response.

    rephrase_observation only reads ``.content[*].text`` and
    ``.usage.*_tokens``, so dict-shaped blocks are sufficient.
    """

    def __init__(self, text: str, input_tokens: int = 120, output_tokens: int = 60) -> None:
        self.content = [{"text": text}]
        self.usage = {"input_tokens": input_tokens, "output_tokens": output_tokens}


class _FakeMessages:
    def __init__(self, parent: _FakeAnthropicClient) -> None:
        self._parent = parent

    def create(self, **kwargs: object) -> _FakeMessage:
        self._parent.calls.append(kwargs)
        if self._parent.raise_exc is not None:
            raise self._parent.raise_exc
        return self._parent.response


class _FakeAnthropicClient:
    def __init__(
        self,
        response_text: str,
        *,
        raise_exc: Exception | None = None,
        input_tokens: int = 120,
        output_tokens: int = 60,
    ) -> None:
        self.response = _FakeMessage(response_text, input_tokens, output_tokens)
        self.raise_exc = raise_exc
        self.calls: list[dict[str, object]] = []
        self.messages = _FakeMessages(self)


class _FakeAnthropicSdk:
    """The ``anthropic`` module surface rephrase_observation touches."""

    def __init__(self, response_text: str, **client_kwargs: object) -> None:
        self._response_text = response_text
        self._client_kwargs = client_kwargs
        self.last_client: _FakeAnthropicClient | None = None

    def Anthropic(self, **kwargs: object) -> _FakeAnthropicClient:  # noqa: N802 (SDK shape)
        self.last_client = _FakeAnthropicClient(self._response_text, **self._client_kwargs)
        return self.last_client


@pytest.fixture
def fake_rephrase_sdk(monkeypatch: pytest.MonkeyPatch):
    """Install a fake anthropic SDK + API key. Returns a setter."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-real")

    def _install(response_text: str, **client_kwargs: object) -> _FakeAnthropicSdk:
        sdk = _FakeAnthropicSdk(response_text, **client_kwargs)
        monkeypatch.setattr(ac_mod, "_sdk_module", lambda: sdk)
        return sdk

    return _install


def test_rephrase_observation_ok_path(fake_rephrase_sdk) -> None:
    polished = (
        "今日最值得留意的是新能源汽车政策转弱，avg_impact=-0.388，"
        "近期与 ETF 资金面方向一致。"
    )
    sdk = fake_rephrase_sdk(polished, input_tokens=140, output_tokens=70)

    result = rephrase_observation(
        RAW_OBSERVATION,
        {"date": "2026-05-17", "industries": ["新能源汽车"]},
        model="fake-model",
    )

    assert result.ok
    assert result.status == "ok"
    assert result.polished_text == polished
    assert result.input_tokens == 140
    assert result.output_tokens == 70
    assert result.llm_model_used == "fake-model"
    # The SDK was actually invoked with our model + user message.
    assert sdk.last_client is not None
    create_kwargs = sdk.last_client.calls[0]
    assert create_kwargs["model"] == "fake-model"
    assert "新能源汽车" in create_kwargs["messages"][0]["content"]


def test_rephrase_observation_dropped_number_falls_back(fake_rephrase_sdk) -> None:
    # The polish drops "-0.388" — the numeric guard must reject it.
    fake_rephrase_sdk("今日最值得留意的是新能源汽车政策转弱。")

    result = rephrase_observation(
        RAW_OBSERVATION, {"industries": ["新能源汽车"]}, model="fake-model"
    )

    assert not result.ok
    assert result.status == "validation_failed"
    assert result.polished_text == RAW_OBSERVATION
    assert result.note is not None and "numbers missing" in result.note


def test_rephrase_observation_overlong_polish_falls_back(fake_rephrase_sdk) -> None:
    overlong = "新能源汽车 avg_impact=-0.388 " + "信号延续" * ac_mod.MAX_POLISHED_CHARS
    fake_rephrase_sdk(overlong)

    result = rephrase_observation(
        RAW_OBSERVATION, {"industries": ["新能源汽车"]}, model="fake-model"
    )

    assert result.status == "too_long"
    assert result.polished_text == RAW_OBSERVATION


def test_rephrase_observation_api_exception_falls_back(fake_rephrase_sdk) -> None:
    fake_rephrase_sdk("unused", raise_exc=RuntimeError("rate limited"))

    result = rephrase_observation(
        RAW_OBSERVATION, {"industries": ["新能源汽车"]}, model="fake-model"
    )

    assert result.status == "api_error"
    assert result.polished_text == RAW_OBSERVATION
    assert result.note is not None and "RuntimeError" in result.note


def test_rephrase_observation_empty_response_falls_back(fake_rephrase_sdk) -> None:
    fake_rephrase_sdk("")

    result = rephrase_observation(
        RAW_OBSERVATION, {"industries": ["新能源汽车"]}, model="fake-model"
    )

    assert result.status == "api_error"
    assert result.polished_text == RAW_OBSERVATION
    assert result.note == "empty response from model"


def test_rephrase_observation_sdk_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ac_mod, "_sdk_module", lambda: None)

    result = rephrase_observation(RAW_OBSERVATION, model="fake-model")

    assert result.status == "sdk_missing"
    assert result.polished_text == RAW_OBSERVATION


def test_rephrase_observation_api_key_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    # Pretend the SDK is installed so we reach the api_key_missing branch.
    monkeypatch.setattr(ac_mod, "_sdk_module", lambda: object())

    result = rephrase_observation(RAW_OBSERVATION, model="fake-model")

    assert result.status == "api_key_missing"
    assert result.polished_text == RAW_OBSERVATION
