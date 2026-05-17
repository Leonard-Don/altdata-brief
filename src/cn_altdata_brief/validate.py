"""Data-quality preconditions check, doctor-style.

Run via ``cn-altdata-brief validate``. The intent is to short-circuit
the daily pipeline BEFORE publishing a brief that would be empty,
stale, or misleading.

The checks are deliberately conservative — they should pass on a
healthy day and fail loudly when something is structurally wrong
upstream (cache stale, file missing, all-zero signals).
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from cn_altdata_brief.adapters.base import (
    AdapterBase,
    AdapterPayload,
    AdapterUnavailable,
    SourceResolution,
)
from cn_altdata_brief.config import public_summary_path

# Severity levels --------------------------------------------------------
INFO = "info"
WARN = "warn"
FAIL = "fail"

# Exit codes (mirror index-inclusion's doctor convention).
EXIT_OK = 0
EXIT_WARN = 1
EXIT_FAIL = 2

# Tunable thresholds — surface them as module-level constants so the
# upstream operator can monkeypatch in tests and lift them via env vars
# in a future revision without spelunking through the check fns.
MIN_POLICY_INDUSTRIES = 3
MIN_MACRO_METALS = 2
MAX_ETF_SNAPSHOT_AGE_DAYS = 7
REQUIRED_HYPOTHESIS_COUNT = 7
PUBLIC_SUMMARY_FRESH_HOURS = 24

# Public-summary sources the freshness check covers. Order is stable so the
# emitted CheckResult.detail dict is deterministic.
#
# v0.4: all four adapters now have a public-summary path. The freshness
# check tolerates per-source absence as WARN, never FAIL — the brief can
# still ship in --source-mode=auto by falling through to the cache.
PUBLIC_SUMMARY_SOURCES: tuple[str, ...] = (
    "super_pricing",
    "index_research",
    "quant_trading",
    "etf_512400",
)


@dataclass(slots=True)
class CheckResult:
    """One precondition's verdict.

    The serialized form intentionally mirrors index-inclusion's
    ``doctor`` JSON so a unified dashboard can later consume both.
    """

    name: str
    level: str  # "info" | "warn" | "fail"
    message: str
    detail: dict[str, Any] | None = None

    def to_line(self) -> str:
        symbol = {INFO: "OK  ", WARN: "WARN", FAIL: "FAIL"}.get(self.level, "????")
        return f"[{symbol}] {self.name}: {self.message}"


# ----------------------------------------------------------------------


def run_all_checks(
    payloads: dict[str, AdapterPayload | None],
    *,
    public_summary_paths: dict[str, Path] | None = None,
    allow_missing_cache_only_sources: bool = False,
) -> list[CheckResult]:
    """Apply all data-quality predicates against the loaded payloads.

    The same payload dict the CLI feeds into ``_synthesize`` is reused
    here — keep validate cheap and idempotent.

    ``public_summary_paths`` lets tests inject custom paths for the new
    ``public_summary_freshness`` check. In production the paths are
    resolved from :func:`cn_altdata_brief.config.public_summary_path`.
    """
    results: list[CheckResult] = []
    results.append(_check_policy_industries(payloads.get("super_pricing")))
    results.append(_check_macro_metals(payloads.get("super_pricing")))
    results.append(
        _check_etf_snapshot_age(
            payloads.get("etf_512400"),
            missing_level=WARN if allow_missing_cache_only_sources else FAIL,
        )
    )
    results.append(_check_verdict_completeness(payloads.get("index_research")))
    results.append(_check_public_summary_freshness(public_summary_paths))
    return results


def summarize(results: list[CheckResult], *, fail_on_warn: bool = False) -> int:
    """Reduce a list of CheckResults to an exit code.

    ``fail_on_warn=True`` upgrades any WARN to a non-zero exit (used by CI).
    Without it, WARN exits with code 1 and only FAIL escalates to 2.
    """
    has_fail = any(r.level == FAIL for r in results)
    has_warn = any(r.level == WARN for r in results)
    if has_fail:
        return EXIT_FAIL
    if has_warn:
        return EXIT_FAIL if fail_on_warn else EXIT_WARN
    return EXIT_OK


# -- individual checks --------------------------------------------------


def _check_policy_industries(payload: AdapterPayload | None) -> CheckResult:
    name = "policy_radar.industries_with_mentions"
    if payload is None:
        return CheckResult(
            name=name,
            level=FAIL,
            message="super-pricing adapter returned no payload (cache missing or unreadable).",
        )
    policy = (payload.data.get("policy_radar") or {}).get("industry_signals") or []
    with_mentions = [row for row in policy if int(row.get("mentions", 0) or 0) > 0]
    detail: dict[str, Any] = {
        "min_required": MIN_POLICY_INDUSTRIES,
        "actual": len(with_mentions),
        "industries": [r.get("industry") for r in with_mentions[:5]],
    }
    if len(with_mentions) < MIN_POLICY_INDUSTRIES:
        return CheckResult(
            name=name,
            level=FAIL,
            message=(
                f"only {len(with_mentions)} industries with mentions "
                f"(need ≥{MIN_POLICY_INDUSTRIES}); brief would publish empty."
            ),
            detail=detail,
        )
    return CheckResult(
        name=name,
        level=INFO,
        message=f"{len(with_mentions)} industries carry policy mentions.",
        detail=detail,
    )


def _check_macro_metals(payload: AdapterPayload | None) -> CheckResult:
    name = "macro_hf.metals_with_weekly_change"
    if payload is None:
        return CheckResult(
            name=name,
            level=FAIL,
            message="super-pricing adapter returned no payload (macro_hf cache missing).",
        )
    metals = (payload.data.get("macro_hf") or {}).get("metals") or []
    valid = [
        m
        for m in metals
        if m.get("price_change_pct") is not None
        and not _is_nan(m.get("price_change_pct"))
    ]
    detail = {
        "min_required": MIN_MACRO_METALS,
        "actual": len(valid),
        "metals": [m.get("name_cn") for m in valid],
    }
    if len(valid) < MIN_MACRO_METALS:
        return CheckResult(
            name=name,
            level=FAIL,
            message=(
                f"only {len(valid)} metals with usable weekly_change_pct "
                f"(need ≥{MIN_MACRO_METALS})."
            ),
            detail=detail,
        )
    return CheckResult(
        name=name,
        level=INFO,
        message=f"{len(valid)} metals carry weekly_change_pct data.",
        detail=detail,
    )


def _check_etf_snapshot_age(
    payload: AdapterPayload | None,
    *,
    missing_level: str = FAIL,
) -> CheckResult:
    name = "etf_512400.snapshot_age"
    if payload is None:
        return CheckResult(
            name=name,
            level=missing_level,
            message=(
                "ETF 512400 snapshot missing; run `npm run refresh` upstream."
                if missing_level == FAIL
                else "ETF 512400 snapshot missing; public-summary mode continues without cache-only ETF data."
            ),
        )
    trade_date_raw = (
        payload.data.get("trade_date")
        or (payload.data.get("nav") or {}).get("date")
        or payload.data.get("generated_at")
    )
    trade_dt = _parse_date(trade_date_raw)
    if trade_dt is None:
        return CheckResult(
            name=name,
            level=WARN,
            message=f"could not parse snapshot date (raw={trade_date_raw!r}).",
            detail={"raw": trade_date_raw},
        )
    today = datetime.now(UTC).date()
    age = (today - trade_dt).days
    detail = {
        "trade_date": trade_dt.isoformat(),
        "today_utc": today.isoformat(),
        "age_days": age,
        "max_allowed": MAX_ETF_SNAPSHOT_AGE_DAYS,
    }
    if age > MAX_ETF_SNAPSHOT_AGE_DAYS:
        return CheckResult(
            name=name,
            level=WARN,
            message=f"ETF snapshot is {age} days old (max {MAX_ETF_SNAPSHOT_AGE_DAYS}); consider refreshing.",
            detail=detail,
        )
    return CheckResult(
        name=name,
        level=INFO,
        message=f"ETF snapshot fresh ({age}d old, max {MAX_ETF_SNAPSHOT_AGE_DAYS}).",
        detail=detail,
    )


def _check_verdict_completeness(payload: AdapterPayload | None) -> CheckResult:
    name = "index_research.verdict_completeness"
    if payload is None:
        return CheckResult(
            name=name,
            level=FAIL,
            message="index-inclusion-research adapter returned no payload (CSV missing).",
        )
    verdicts = payload.data.get("verdicts") or []
    actual = len(verdicts)
    detail = {
        "required": REQUIRED_HYPOTHESIS_COUNT,
        "actual": actual,
        "hypothesis_ids": [v.get("hid") for v in verdicts],
    }
    if actual < REQUIRED_HYPOTHESIS_COUNT:
        return CheckResult(
            name=name,
            level=FAIL,
            message=(
                f"only {actual}/{REQUIRED_HYPOTHESIS_COUNT} hypothesis verdicts present; "
                "verdicts CSV is incomplete."
            ),
            detail=detail,
        )
    if actual > REQUIRED_HYPOTHESIS_COUNT:
        # More than 7 isn't a hard fail (the research project might add H8) but
        # the brief's narrative was tuned for exactly 7 — flag as a soft warn.
        return CheckResult(
            name=name,
            level=WARN,
            message=f"{actual} hypothesis verdicts present (expected exactly {REQUIRED_HYPOTHESIS_COUNT}); narrative tuning may need refresh.",
            detail=detail,
        )
    return CheckResult(
        name=name,
        level=INFO,
        message=f"all {REQUIRED_HYPOTHESIS_COUNT} hypothesis verdicts present.",
        detail=detail,
    )


def _check_public_summary_freshness(
    paths_override: dict[str, Path] | None = None,
) -> CheckResult:
    """Verify each expected public summary file exists and is fresh.

    Severity:
        * INFO  — every expected file exists with a ``generated_at`` within
          ``PUBLIC_SUMMARY_FRESH_HOURS``.
        * WARN  — a file is missing OR ``generated_at`` is older than the
          freshness window OR unparsable.
        * (never FAIL — the brief can still publish from cache; this check
          exists to flag the GitHub Actions path's input freshness.)
    """
    name = "public_summary_freshness"
    paths: dict[str, Path] = {}
    if paths_override is not None:
        paths = {source_key: Path(path) for source_key, path in paths_override.items()}
    else:
        for source_key in PUBLIC_SUMMARY_SOURCES:
            try:
                paths[source_key] = public_summary_path(source_key)
            except KeyError:  # pragma: no cover - defensive
                continue

    per_source: dict[str, dict[str, Any]] = {}
    now = datetime.now(UTC)
    threshold = timedelta(hours=PUBLIC_SUMMARY_FRESH_HOURS)
    worst_level = INFO
    messages: list[str] = []

    for source_key, path in paths.items():
        entry: dict[str, Any] = {"path": str(path)}
        if not path.exists():
            entry.update({"present": False, "age_hours": None, "generated_at": None})
            messages.append(f"{source_key}: missing")
            worst_level = _escalate(worst_level, WARN)
            per_source[source_key] = entry
            continue

        entry["present"] = True
        generated_at: str | None = None
        try:
            with path.open(encoding="utf-8") as fh:
                doc = json.load(fh)
        except (OSError, json.JSONDecodeError):
            entry.update({"generated_at": None, "age_hours": None, "parse_error": True})
            messages.append(f"{source_key}: unreadable")
            worst_level = _escalate(worst_level, WARN)
            per_source[source_key] = entry
            continue

        if isinstance(doc, dict):
            generated_at = doc.get("generated_at")
            # ETF 512400 liveSnapshot ships generatedAt under meta — accept it
            # as a freshness signal so all four adapters share the same check.
            if generated_at is None and isinstance(doc.get("meta"), dict):
                generated_at = doc["meta"].get("generatedAt")
        entry["generated_at"] = generated_at

        ts = _parse_iso_timestamp(generated_at)
        if ts is None:
            entry["age_hours"] = None
            messages.append(f"{source_key}: unparsable generated_at={generated_at!r}")
            worst_level = _escalate(worst_level, WARN)
            per_source[source_key] = entry
            continue

        age = now - ts
        entry["age_hours"] = round(age.total_seconds() / 3600.0, 2)
        if age > threshold:
            messages.append(
                f"{source_key}: stale ({entry['age_hours']}h > {PUBLIC_SUMMARY_FRESH_HOURS}h)"
            )
            worst_level = _escalate(worst_level, WARN)
        else:
            messages.append(f"{source_key}: ok ({entry['age_hours']}h)")
        per_source[source_key] = entry

    detail = {
        "freshness_window_hours": PUBLIC_SUMMARY_FRESH_HOURS,
        "sources": per_source,
    }
    if worst_level == INFO:
        return CheckResult(
            name=name,
            level=INFO,
            message=f"all public summaries fresh within {PUBLIC_SUMMARY_FRESH_HOURS}h.",
            detail=detail,
        )
    return CheckResult(
        name=name,
        level=WARN,
        message="; ".join(messages) if messages else "issues found",
        detail=detail,
    )


def _escalate(current: str, incoming: str) -> str:
    rank = {INFO: 0, WARN: 1, FAIL: 2}
    return current if rank.get(current, 0) >= rank.get(incoming, 0) else incoming


def _parse_iso_timestamp(raw: Any) -> datetime | None:
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    try:
        normalized = text.replace("Z", "+00:00")
        dt = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


# -- shared helpers -----------------------------------------------------


def _is_nan(value: Any) -> bool:
    try:
        return math.isnan(float(value))
    except (TypeError, ValueError):
        return False


def _parse_date(raw: Any) -> date | None:
    """Parse YYYY-MM-DD or ISO-8601 timestamps into a ``date``."""
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    # First, try plain date.
    for fmt in ("%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    # Fall back to ISO-8601 (with or without trailing Z).
    try:
        normalized = text.replace("Z", "+00:00")
        return datetime.fromisoformat(normalized).date()
    except ValueError:
        return None


def load_payloads_for_validate() -> dict[str, AdapterPayload | None]:
    """Build the same payload dict the generate command uses.

    Kept here (rather than in cli.py) so that ``run_all_checks`` can be
    exercised directly from tests with monkeypatched paths.
    """
    from cn_altdata_brief.adapters import build_default_adapters

    payloads: dict[str, AdapterPayload | None] = {}
    for name, adapter in build_default_adapters().items():
        try:
            payloads[name] = adapter.fetch()
        except AdapterUnavailable:
            payloads[name] = None
    return payloads


def resolve_all_sources(
    adapters: dict[str, AdapterBase],
) -> dict[str, SourceResolution]:
    """Probe every adapter's resolution without fetching.

    Used by the validate CLI to report a per-adapter line of "which path
    each adapter picked", independent of the actual payload retrieval.
    The dict order matches the input — adapters are iterated in the
    order :func:`build_default_adapters` returned them.
    """
    return {name: adapter.resolve_source() for name, adapter in adapters.items()}


# Re-export for convenience tests.
__all__ = [
    "CheckResult",
    "EXIT_FAIL",
    "EXIT_OK",
    "EXIT_WARN",
    "FAIL",
    "INFO",
    "MAX_ETF_SNAPSHOT_AGE_DAYS",
    "MIN_MACRO_METALS",
    "MIN_POLICY_INDUSTRIES",
    "PUBLIC_SUMMARY_FRESH_HOURS",
    "PUBLIC_SUMMARY_SOURCES",
    "REQUIRED_HYPOTHESIS_COUNT",
    "WARN",
    "load_payloads_for_validate",
    "resolve_all_sources",
    "run_all_checks",
    "summarize",
]


