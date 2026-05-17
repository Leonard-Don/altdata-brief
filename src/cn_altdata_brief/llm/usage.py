"""Append-only LLM usage log + aggregation.

Each successful (or attempted) rephrase appends one JSON line to
``output/llm_usage.jsonl``. The file is human-readable, grep-able, and
easy to load into a notebook for billing reconciliation. We never
overwrite or rotate the file from inside the application — operators
who care can archive it externally.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from cn_altdata_brief.llm.anthropic_client import (
    DEFAULT_INPUT_COST_PER_MTOK,
    DEFAULT_OUTPUT_COST_PER_MTOK,
    RephraseResult,
)

logger = logging.getLogger(__name__)


def estimate_cost_usd(
    input_tokens: int | None,
    output_tokens: int | None,
    input_cost_per_mtok: float = DEFAULT_INPUT_COST_PER_MTOK,
    output_cost_per_mtok: float = DEFAULT_OUTPUT_COST_PER_MTOK,
) -> float:
    """Rough USD estimate for a single rephrase call."""
    i = float(input_tokens or 0)
    o = float(output_tokens or 0)
    return (i * input_cost_per_mtok + o * output_cost_per_mtok) / 1_000_000.0


def log_usage(
    result: RephraseResult,
    log_path: Path,
    *,
    extra: dict[str, Any] | None = None,
) -> None:
    """Append one usage record to ``log_path`` (creates parent dirs).

    Never raises — a write failure logs a warning and proceeds. The
    rephrase result itself is what the user actually wants; the usage
    log is a side-channel that should not block brief generation.
    """
    record: dict[str, Any] = {
        "timestamp": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "model": result.llm_model_used,
        "status": result.status,
        "input_tokens": result.input_tokens,
        "output_tokens": result.output_tokens,
        "latency_ms": round(result.latency_ms, 1) if result.latency_ms is not None else None,
        "est_cost_usd": round(
            estimate_cost_usd(result.input_tokens, result.output_tokens), 6
        ),
        "prompt_hash": result.prompt_hash[:16] if result.prompt_hash else "",
    }
    if extra:
        record.update(extra)

    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError as exc:
        logger.warning("could not append to llm_usage.jsonl (%s)", exc)


@dataclass(slots=True)
class UsageAggregate:
    """Roll-up of ``llm_usage.jsonl`` over the last N days.

    Surface in the CLI's ``llm-usage`` subcommand. ``per_status`` is
    handy for spotting whether validation failures are creeping up.
    """

    days: int
    total_calls: int
    ok_calls: int
    fallback_calls: int
    input_tokens: int
    output_tokens: int
    est_cost_usd: float
    avg_latency_ms: float | None
    per_status: dict[str, int]
    per_model: dict[str, int]
    first_ts: str | None
    last_ts: str | None


def aggregate_usage(
    log_path: Path,
    *,
    days: int | None = None,
    now: datetime | None = None,
) -> UsageAggregate:
    """Load ``log_path`` and aggregate, optionally restricted to the last ``days`` days.

    ``days=None`` returns the lifetime aggregate. ``now`` is injectable
    for deterministic tests.
    """
    now = now or datetime.now(UTC)
    cutoff = (now - timedelta(days=days)) if days else None

    total = ok = fallback = 0
    input_total = output_total = 0
    latencies: list[float] = []
    cost_total = 0.0
    per_status: dict[str, int] = {}
    per_model: dict[str, int] = {}
    first_ts: str | None = None
    last_ts: str | None = None

    if not log_path.exists():
        return UsageAggregate(
            days=days or 0,
            total_calls=0,
            ok_calls=0,
            fallback_calls=0,
            input_tokens=0,
            output_tokens=0,
            est_cost_usd=0.0,
            avg_latency_ms=None,
            per_status={},
            per_model={},
            first_ts=None,
            last_ts=None,
        )

    with log_path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            ts_raw = rec.get("timestamp")
            if cutoff is not None and ts_raw:
                try:
                    ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
                except ValueError:
                    ts = None
                if ts is not None and ts < cutoff:
                    continue

            total += 1
            status = rec.get("status", "unknown")
            per_status[status] = per_status.get(status, 0) + 1
            if status == "ok":
                ok += 1
            else:
                fallback += 1
            model = rec.get("model") or "(none)"
            per_model[model] = per_model.get(model, 0) + 1

            input_total += int(rec.get("input_tokens") or 0)
            output_total += int(rec.get("output_tokens") or 0)
            latency = rec.get("latency_ms")
            if latency is not None:
                try:
                    latencies.append(float(latency))
                except (TypeError, ValueError):
                    pass
            try:
                cost_total += float(rec.get("est_cost_usd") or 0.0)
            except (TypeError, ValueError):
                pass

            if first_ts is None or (ts_raw and ts_raw < first_ts):
                first_ts = ts_raw
            if last_ts is None or (ts_raw and ts_raw > last_ts):
                last_ts = ts_raw

    avg_latency = sum(latencies) / len(latencies) if latencies else None
    return UsageAggregate(
        days=days or 0,
        total_calls=total,
        ok_calls=ok,
        fallback_calls=fallback,
        input_tokens=input_total,
        output_tokens=output_total,
        est_cost_usd=round(cost_total, 4),
        avg_latency_ms=round(avg_latency, 1) if avg_latency is not None else None,
        per_status=per_status,
        per_model=per_model,
        first_ts=first_ts,
        last_ts=last_ts,
    )
