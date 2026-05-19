"""Content-quality validation checks (v0.12).

These complement ``validate.py``'s freshness/structural checks. The
freshness layer answers "is the file recent and has the right number of
rows?". The quality layer answers "is the *content* useful?":

* :func:`check_content_fingerprint_freshness` — same file content for
  >N days is suspicious even when mtime is fresh (cache/proxy reuse).
* :func:`check_signal_density` — count of records that carry an
  actionable signal (non-zero impact / non-zero price change) vs the
  total. Catches "valid JSON, all-zero values".
* :func:`check_cross_source_consistency` — when two sources both have
  an opinion on an industry, do they agree? Disagreement flags
  upstream divergence worth reading before publishing.
* :func:`check_schema_regression` — required keys present, unknown
  keys flagged for review. Baselines live in
  ``tests/fixtures/schemas/<source>.schema.json``.

All check functions return a :class:`cn_altdata_brief.validate.CheckResult`
so the existing ``summarize()`` and CLI rendering work unchanged. New
checks are opt-in via ``--strict`` or the per-check flags — the default
``validate`` CLI run is unchanged to preserve v0.2 backward compatibility.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cn_altdata_brief.adapters.base import AdapterPayload
from cn_altdata_brief.validate import (
    FAIL,
    INFO,
    WARN,
    CheckResult,
)

# Defaults / thresholds. Surface as module-level constants so tests can
# monkeypatch and downstream callers can lift without re-reading source.
SIGNAL_DENSITY_MIN_RATIO = 0.30  # 30 % of rows must carry signal
POLICY_IMPACT_FLOOR = 0.1  # |avg_impact| threshold for "carries signal"
FINGERPRINT_STALE_DAYS = 2  # how many consecutive identical days before WARN

# Project root resolution (this file lives at src/cn_altdata_brief/).
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_FINGERPRINT_HISTORY = _PROJECT_ROOT / "output" / "fingerprint_history.json"
DEFAULT_SCHEMA_DIR = _PROJECT_ROOT / "tests" / "fixtures" / "schemas"

# Each schema baseline file lives at <schema_dir>/<source>.schema.json.
_SCHEMA_FILENAMES: dict[str, str] = {
    "super_pricing": "super_pricing.schema.json",
    "quant_trading": "quant_trading.schema.json",
    "index_research": "index_research.schema.json",
    "etf_512400": "etf_512400.schema.json",
}


# ---------------------------------------------------------------------------
# Fingerprint history I/O
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class FingerprintEntry:
    """One observation in the rolling fingerprint history.

    The check stores the SHA-256 hexdigest of the normalized content and
    the ISO date it was first seen. When today's fingerprint equals an
    older entry's fingerprint, we know the content hasn't changed.
    """

    fingerprint: str
    first_seen: str  # YYYY-MM-DD
    last_seen: str  # YYYY-MM-DD


def load_fingerprint_history(path: Path) -> dict[str, list[FingerprintEntry]]:
    """Read the persisted fingerprint history; tolerate missing/corrupt files.

    The on-disk shape is ``{source_key: [{fingerprint, first_seen, last_seen}, ...]}``.
    Older entries come first; the file is intentionally small — we cap
    history at 14 entries per source to keep the JSON readable.
    """
    if not path.exists():
        return {}
    try:
        with path.open(encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}
    out: dict[str, list[FingerprintEntry]] = {}
    if not isinstance(raw, dict):
        return out
    for key, entries in raw.items():
        if not isinstance(entries, list):
            continue
        loaded: list[FingerprintEntry] = []
        for row in entries:
            if not isinstance(row, dict):
                continue
            fp = str(row.get("fingerprint", "") or "")
            first = str(row.get("first_seen", "") or "")
            last = str(row.get("last_seen", "") or "")
            if not fp or not first:
                continue
            loaded.append(
                FingerprintEntry(
                    fingerprint=fp,
                    first_seen=first,
                    last_seen=last or first,
                )
            )
        if loaded:
            out[str(key)] = loaded
    return out


def save_fingerprint_history(
    path: Path,
    history: dict[str, list[FingerprintEntry]],
    *,
    max_entries: int = 14,
) -> None:
    """Persist fingerprint history with newest entries kept at most ``max_entries``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    out: dict[str, list[dict[str, str]]] = {}
    for key, entries in history.items():
        trimmed = entries[-max_entries:] if len(entries) > max_entries else entries
        out[key] = [
            {
                "fingerprint": e.fingerprint,
                "first_seen": e.first_seen,
                "last_seen": e.last_seen,
            }
            for e in trimmed
        ]
    with path.open("w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=2, sort_keys=True)
        fh.write("\n")


# ---------------------------------------------------------------------------
# Fingerprint check
# ---------------------------------------------------------------------------


def _normalize_policy_records_for_fingerprint(
    payload: AdapterPayload | None,
) -> list[dict[str, Any]]:
    """Reduce policy_radar payload to a stable, hashable list of signals.

    We deliberately strip timestamps and float-formatting noise: the
    fingerprint should change only when *content* changes, not when
    upstream re-emits the same numbers with a new ``generated_at``.
    """
    if payload is None:
        return []
    policy = (payload.data.get("policy_radar") or {})
    rows = policy.get("industry_signals") or []
    # rows can be list[dict] (after adapter normalization) or dict[name -> info]
    if isinstance(rows, dict):
        items = list(rows.items())
        normalized: list[dict[str, Any]] = []
        for name, info in items:
            if not isinstance(info, dict):
                continue
            normalized.append(
                {
                    "industry": str(name),
                    "signal": str(info.get("signal", "")),
                    "impact": round(float(info.get("avg_impact", 0.0) or 0.0), 4),
                }
            )
        normalized.sort(key=lambda r: r["industry"])
        return normalized
    if isinstance(rows, list):
        normalized = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            normalized.append(
                {
                    "industry": str(row.get("industry", "")),
                    "signal": str(row.get("signal", "")),
                    "impact": round(float(row.get("avg_impact", 0.0) or 0.0), 4),
                }
            )
        normalized.sort(key=lambda r: r["industry"])
        return normalized
    return []


def compute_policy_fingerprint(payload: AdapterPayload | None) -> str:
    """Stable sha256 hexdigest of the policy_radar signal content."""
    rows = _normalize_policy_records_for_fingerprint(payload)
    canonical = json.dumps(rows, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def update_fingerprint_history(
    history: dict[str, list[FingerprintEntry]],
    *,
    source_key: str,
    fingerprint: str,
    today: str,
) -> tuple[int, str]:
    """Apply today's fingerprint to a source's history.

    Returns ``(consecutive_days_unchanged, action)`` where ``action`` is
    one of:

    * ``"new"`` — fingerprint not previously seen; appended.
    * ``"extended"`` — fingerprint matches the most recent entry's;
      its ``last_seen`` is updated to today (history length unchanged).

    ``consecutive_days_unchanged`` is the inclusive span between
    ``first_seen`` and ``today`` for the matching entry, in calendar
    days. For brand-new fingerprints this is 1.
    """
    entries = history.setdefault(source_key, [])
    if entries and entries[-1].fingerprint == fingerprint:
        # Extend the run.
        entries[-1].last_seen = today
        first = entries[-1].first_seen
        return _inclusive_day_span(first, today), "extended"
    entries.append(
        FingerprintEntry(fingerprint=fingerprint, first_seen=today, last_seen=today)
    )
    return 1, "new"


def _inclusive_day_span(first: str, last: str) -> int:
    """Number of calendar days inclusive between two YYYY-MM-DD strings."""
    try:
        d1 = datetime.strptime(first, "%Y-%m-%d").date()
        d2 = datetime.strptime(last, "%Y-%m-%d").date()
    except ValueError:
        return 1
    return max(1, (d2 - d1).days + 1)


def check_content_fingerprint_freshness(
    payloads: dict[str, AdapterPayload | None],
    *,
    history_path: Path | None = None,
    today: str | None = None,
    persist: bool = True,
    stale_after_days: int = FINGERPRINT_STALE_DAYS,
) -> CheckResult:
    """Detect stale-as-fresh data: identical content for >N days.

    Reads/writes ``output/fingerprint_history.json``. Currently watches
    only ``policy_radar`` (the most fingerprint-able source — a small,
    stable list of industries × signals). Other sources can be added
    later by extending the SOURCES table here.
    """
    name = "content_fingerprint_freshness"
    history_path = history_path if history_path else DEFAULT_FINGERPRINT_HISTORY
    today = today or datetime.now(UTC).strftime("%Y-%m-%d")

    history = load_fingerprint_history(history_path)
    per_source: dict[str, dict[str, Any]] = {}

    SOURCES: tuple[tuple[str, str, Any], ...] = (
        ("super_pricing", "policy_radar", _normalize_policy_records_for_fingerprint),
    )

    stalest_days = 0
    stalest_source: str | None = None
    for source_key, scope, _normalizer in SOURCES:
        payload = payloads.get(source_key)
        fp = compute_policy_fingerprint(payload)
        rows = _normalize_policy_records_for_fingerprint(payload)
        if not rows:
            # Treat empty content as "skip" — no signal to fingerprint.
            per_source[source_key] = {
                "scope": scope,
                "fingerprint": fp,
                "consecutive_days": 0,
                "action": "skipped_empty",
            }
            continue
        consecutive, action = update_fingerprint_history(
            history, source_key=source_key, fingerprint=fp, today=today
        )
        per_source[source_key] = {
            "scope": scope,
            "fingerprint": fp[:16],  # short prefix is enough for the report
            "consecutive_days": consecutive,
            "action": action,
        }
        if consecutive > stalest_days:
            stalest_days = consecutive
            stalest_source = source_key

    if persist:
        save_fingerprint_history(history_path, history)

    detail = {
        "history_path": str(history_path),
        "today": today,
        "stale_after_days": stale_after_days,
        "sources": per_source,
    }

    if stalest_source and stalest_days > stale_after_days:
        return CheckResult(
            name=name,
            level=WARN,
            message=(
                f"{stalest_source} content unchanged for {stalest_days} days "
                f"(threshold {stale_after_days}); upstream may be serving cached data."
            ),
            detail=detail,
        )
    if stalest_source is None:
        return CheckResult(
            name=name,
            level=INFO,
            message="no fingerprintable content (empty policy_radar); skipped.",
            detail=detail,
        )
    return CheckResult(
        name=name,
        level=INFO,
        message=(
            f"content fresh ({stalest_source} unchanged {stalest_days}d, "
            f"threshold {stale_after_days})."
        ),
        detail=detail,
    )


# ---------------------------------------------------------------------------
# Signal density check
# ---------------------------------------------------------------------------


def _is_nan(value: Any) -> bool:
    try:
        return math.isnan(float(value))
    except (TypeError, ValueError):
        return False


def _signal_density_policy(payload: AdapterPayload | None) -> tuple[int, int]:
    """Return ``(rows_with_signal, total_rows)`` for policy_radar.

    A row "carries signal" when ``|avg_impact| > POLICY_IMPACT_FLOOR``.
    """
    if payload is None:
        return (0, 0)
    rows = (payload.data.get("policy_radar") or {}).get("industry_signals") or []
    # Accept dict OR list shape, mirroring fingerprint normalization.
    if isinstance(rows, dict):
        iterable: Iterable[dict[str, Any]] = (
            {"industry": k, **(v or {})} if isinstance(v, dict) else {}
            for k, v in rows.items()
        )
    else:
        iterable = (r for r in rows if isinstance(r, dict))
    total = 0
    with_signal = 0
    for row in iterable:
        total += 1
        try:
            impact = float(row.get("avg_impact", 0.0) or 0.0)
        except (TypeError, ValueError):
            impact = 0.0
        if abs(impact) > POLICY_IMPACT_FLOOR:
            with_signal += 1
    return (with_signal, total)


def _signal_density_macro(payload: AdapterPayload | None) -> tuple[int, int]:
    """``(metals_with_signal, total_metals)`` — metals with nonzero, non-NaN weekly change."""
    if payload is None:
        return (0, 0)
    metals = (payload.data.get("macro_hf") or {}).get("metals") or []
    total = 0
    with_signal = 0
    for m in metals:
        if not isinstance(m, dict):
            continue
        total += 1
        raw = m.get("price_change_pct")
        if raw is None or _is_nan(raw):
            continue
        try:
            val = float(raw)
        except (TypeError, ValueError):
            continue
        if val != 0.0:
            with_signal += 1
    return (with_signal, total)


def check_signal_density(
    payloads: dict[str, AdapterPayload | None],
    *,
    min_ratio: float = SIGNAL_DENSITY_MIN_RATIO,
) -> CheckResult:
    """WARN if too few records carry an actionable signal.

    Computes density independently for ``policy_radar`` (impact magnitude)
    and ``macro_hf`` (non-zero weekly price change). The WORST of the two
    drives the verdict so a healthy source can't mask a degraded one.
    """
    name = "signal_density"
    sp = payloads.get("super_pricing")
    p_with, p_total = _signal_density_policy(sp)
    m_with, m_total = _signal_density_macro(sp)

    def _ratio(num: int, den: int) -> float | None:
        if den == 0:
            return None
        return num / den

    p_ratio = _ratio(p_with, p_total)
    m_ratio = _ratio(m_with, m_total)
    detail = {
        "min_ratio": min_ratio,
        "policy_radar": {
            "with_signal": p_with,
            "total": p_total,
            "ratio": p_ratio,
        },
        "macro_hf": {
            "with_signal": m_with,
            "total": m_total,
            "ratio": m_ratio,
        },
    }
    breaches: list[str] = []
    if p_ratio is not None and p_ratio < min_ratio:
        breaches.append(
            f"policy_radar density {p_ratio:.0%} ({p_with}/{p_total}) < {min_ratio:.0%}"
        )
    if m_ratio is not None and m_ratio < min_ratio:
        breaches.append(
            f"macro_hf density {m_ratio:.0%} ({m_with}/{m_total}) < {min_ratio:.0%}"
        )

    if p_total == 0 and m_total == 0:
        return CheckResult(
            name=name,
            level=WARN,
            message="no policy_radar or macro_hf rows available to score density.",
            detail=detail,
        )
    if breaches:
        return CheckResult(
            name=name,
            level=WARN,
            message="; ".join(breaches),
            detail=detail,
        )
    summary_parts = []
    if p_ratio is not None:
        summary_parts.append(f"policy={p_ratio:.0%}({p_with}/{p_total})")
    if m_ratio is not None:
        summary_parts.append(f"macro={m_ratio:.0%}({m_with}/{m_total})")
    return CheckResult(
        name=name,
        level=INFO,
        message="signal density healthy · " + " · ".join(summary_parts),
        detail=detail,
    )


# ---------------------------------------------------------------------------
# Cross-source consistency check
# ---------------------------------------------------------------------------


def _industry_signal_from_policy(
    payload: AdapterPayload | None,
) -> dict[str, str]:
    """Map industry -> ``bullish/bearish/neutral`` from policy_radar.

    Accepts both list-of-rows (adapter-normalized) and dict-of-rows (raw)
    shapes; missing/empty signals are skipped.
    """
    out: dict[str, str] = {}
    if payload is None:
        return out
    rows = (payload.data.get("policy_radar") or {}).get("industry_signals") or []
    if isinstance(rows, dict):
        for name, info in rows.items():
            if not isinstance(info, dict):
                continue
            sig = str(info.get("signal", "")).strip().lower()
            if sig:
                out[str(name).strip()] = sig
    elif isinstance(rows, list):
        for row in rows:
            if not isinstance(row, dict):
                continue
            sig = str(row.get("signal", "")).strip().lower()
            name = str(row.get("industry", "")).strip()
            if name and sig:
                out[name] = sig
    return out


def _industry_signal_from_quant(
    payload: AdapterPayload | None,
) -> dict[str, str]:
    """Map industry -> direction from quant_trading.

    Heat alone doesn't carry direction, so we use the policy overlay
    field ``policy_signal`` when present. When only ``change_pct`` is
    available (the upstream's newer shape), we map +/- to bullish/bearish.
    """
    out: dict[str, str] = {}
    if payload is None:
        return out
    rows = payload.data.get("industries") or []
    if not isinstance(rows, list):
        return out
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = (
            str(row.get("industry") or row.get("industry_name") or "").strip()
        )
        if not name:
            continue
        sig = str(row.get("policy_signal", "")).strip().lower()
        if not sig or sig == "neutral":
            # Try change_pct as a directional proxy if the upstream shipped one.
            cp = row.get("change_pct")
            try:
                cpv = float(cp) if cp is not None else None
            except (TypeError, ValueError):
                cpv = None
            if cpv is not None:
                if cpv > 0.5:
                    sig = "bullish"
                elif cpv < -0.5:
                    sig = "bearish"
                else:
                    sig = "neutral"
        if sig:
            out[name] = sig
    return out


def _opposing(a: str, b: str) -> bool:
    """True iff ``a`` and ``b`` are non-neutral and pointing opposite ways."""
    pairs = {("bullish", "bearish"), ("bearish", "bullish")}
    return (a, b) in pairs


def check_cross_source_consistency(
    payloads: dict[str, AdapterPayload | None],
) -> CheckResult:
    """Compare per-industry signal direction across sources.

    INFO when only one source has an opinion on an industry, or all
    agree. WARN when two sources have opposing directions on the same
    industry (real divergence the operator should read before publishing).
    """
    name = "cross_source_consistency"
    policy = _industry_signal_from_policy(payloads.get("super_pricing"))
    quant = _industry_signal_from_quant(payloads.get("quant_trading"))

    conflicts: list[dict[str, str]] = []
    agreements: list[dict[str, str]] = []
    shared_industries = set(policy) & set(quant)
    for ind in sorted(shared_industries):
        p = policy.get(ind, "neutral")
        q = quant.get(ind, "neutral")
        if _opposing(p, q):
            conflicts.append({"industry": ind, "policy": p, "quant": q})
        else:
            agreements.append({"industry": ind, "policy": p, "quant": q})

    detail = {
        "policy_signals": policy,
        "quant_signals": quant,
        "shared_industries": sorted(shared_industries),
        "conflicts": conflicts,
        "agreements": agreements,
    }
    if conflicts:
        first = conflicts[0]
        more = f" (+ {len(conflicts) - 1} more)" if len(conflicts) > 1 else ""
        return CheckResult(
            name=name,
            level=WARN,
            message=(
                f"conflict on {first['industry']}: "
                f"policy={first['policy']} vs quant={first['quant']}{more}"
            ),
            detail=detail,
        )
    if not shared_industries:
        return CheckResult(
            name=name,
            level=INFO,
            message="no industries observed by both policy_radar and quant_trading; nothing to compare.",
            detail=detail,
        )
    return CheckResult(
        name=name,
        level=INFO,
        message=f"{len(shared_industries)} shared industries all agree.",
        detail=detail,
    )


# ---------------------------------------------------------------------------
# Schema regression check
# ---------------------------------------------------------------------------


def load_schema_baseline(
    source_key: str, *, schema_dir: Path | None = None
) -> dict[str, Any] | None:
    """Read the schema baseline for a source; return ``None`` when absent."""
    schema_dir = schema_dir if schema_dir else DEFAULT_SCHEMA_DIR
    filename = _SCHEMA_FILENAMES.get(source_key)
    if filename is None:
        return None
    path = schema_dir / filename
    if not path.exists():
        return None
    try:
        with path.open(encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None


def _walk_expected(
    payload_section: dict[str, Any],
    expected: dict[str, Any],
) -> tuple[list[str], list[str]]:
    """Compare ``payload_section`` keys to ``expected`` (required + optional).

    Returns ``(missing_required, unknown_keys)`` — flat lists of key
    names. ``expected`` is the ``{"required": [...], "optional": [...]}``
    block from the schema baseline.
    """
    required = set(expected.get("required") or [])
    optional = set(expected.get("optional") or [])
    payload_keys = set(payload_section.keys()) if isinstance(payload_section, dict) else set()
    missing = sorted(required - payload_keys)
    unknown = sorted(payload_keys - required - optional)
    return missing, unknown


def check_schema_regression(
    payloads: dict[str, AdapterPayload | None],
    *,
    schema_dir: Path | None = None,
) -> CheckResult:
    """Compare each adapter's data shape to a committed baseline.

    Severity rules:

    * **FAIL** — at least one required key is missing.
    * **INFO** — only unknown keys appeared (schema evolution upstream;
      worth a heads-up but not a blocker).
    * **INFO** — baselines all match exactly.
    * **WARN** — payload missing for a source that has a baseline.

    When a baseline file is missing for a source we silently skip that
    source — the test harness uses ``schema_dir=tmp`` to opt out.
    """
    name = "schema_regression"
    schema_dir = schema_dir if schema_dir else DEFAULT_SCHEMA_DIR

    fails: list[str] = []
    warns: list[str] = []
    per_source: dict[str, dict[str, Any]] = {}

    for source_key in _SCHEMA_FILENAMES.keys():
        baseline = load_schema_baseline(source_key, schema_dir=schema_dir)
        if not baseline:
            continue
        payload = payloads.get(source_key)
        per_source[source_key] = {
            "baseline_version": baseline.get("baseline_version"),
        }
        if payload is None:
            warns.append(f"{source_key} payload missing")
            per_source[source_key]["status"] = "payload_missing"
            continue

        expected = baseline.get("expected_payload_keys") or {}
        all_missing: list[str] = []
        all_unknown: list[str] = []
        for section_key, section_expected in expected.items():
            if not isinstance(section_expected, dict):
                continue
            if section_key == "_root":
                section_data = payload.data
            else:
                section_data = (payload.data.get(section_key) or {})
                # Some adapters nest under data.<section>; if not a dict, skip
                # with a synthetic "missing_required" — the section vanished.
                if not isinstance(section_data, dict):
                    for req in section_expected.get("required") or []:
                        all_missing.append(f"{section_key}.{req}")
                    continue
            missing, unknown = _walk_expected(section_data, section_expected)
            all_missing.extend(f"{section_key}.{k}" for k in missing)
            all_unknown.extend(f"{section_key}.{k}" for k in unknown)
        per_source[source_key].update(
            {
                "missing_required": all_missing,
                "unknown_keys": all_unknown,
            }
        )
        if all_missing:
            fails.append(
                f"{source_key} missing required: {', '.join(all_missing[:3])}"
                + (f" (+ {len(all_missing) - 3} more)" if len(all_missing) > 3 else "")
            )

    detail = {
        "schema_dir": str(schema_dir),
        "per_source": per_source,
    }

    if fails:
        return CheckResult(
            name=name,
            level=FAIL,
            message="; ".join(fails),
            detail=detail,
        )
    if warns:
        return CheckResult(
            name=name,
            level=WARN,
            message="; ".join(warns),
            detail=detail,
        )
    if not per_source:
        return CheckResult(
            name=name,
            level=INFO,
            message="no schema baselines loaded; nothing to compare.",
            detail=detail,
        )
    # Any source with unknown keys surfaces those in the message so the
    # operator notices schema evolution. The verdict stays INFO since
    # unknown keys are not blockers; they're a heads-up for the next baseline bump.
    sources_with_unknown = [
        f"{src} has unknown keys (schema evolution?): {', '.join(meta['unknown_keys'][:3])}"
        + (
            f" (+ {len(meta['unknown_keys']) - 3} more)"
            if len(meta["unknown_keys"]) > 3
            else ""
        )
        for src, meta in per_source.items()
        if meta.get("unknown_keys")
    ]
    if sources_with_unknown:
        return CheckResult(
            name=name,
            level=INFO,
            message="; ".join(sources_with_unknown),
            detail=detail,
        )
    return CheckResult(
        name=name,
        level=INFO,
        message="all loaded schema baselines match payloads.",
        detail=detail,
    )


# ---------------------------------------------------------------------------
# Strict-mode aggregator
# ---------------------------------------------------------------------------


def run_strict_checks(
    payloads: dict[str, AdapterPayload | None],
    *,
    history_path: Path | None = None,
    schema_dir: Path | None = None,
    today: str | None = None,
    persist: bool = True,
    include: tuple[str, ...] | None = None,
) -> list[CheckResult]:
    """Run any subset of the four quality checks.

    ``include`` is a tuple of check identifiers — when ``None``, runs all
    four. Identifiers map to: ``"fingerprint"``, ``"density"``,
    ``"consistency"``, ``"schema"``.
    """
    include = include if include is not None else (
        "fingerprint",
        "density",
        "consistency",
        "schema",
    )
    out: list[CheckResult] = []
    if "fingerprint" in include:
        out.append(
            check_content_fingerprint_freshness(
                payloads,
                history_path=history_path,
                today=today,
                persist=persist,
            )
        )
    if "density" in include:
        out.append(check_signal_density(payloads))
    if "consistency" in include:
        out.append(check_cross_source_consistency(payloads))
    if "schema" in include:
        out.append(check_schema_regression(payloads, schema_dir=schema_dir))
    return out


__all__ = [
    "DEFAULT_FINGERPRINT_HISTORY",
    "DEFAULT_SCHEMA_DIR",
    "FINGERPRINT_STALE_DAYS",
    "FingerprintEntry",
    "POLICY_IMPACT_FLOOR",
    "SIGNAL_DENSITY_MIN_RATIO",
    "check_content_fingerprint_freshness",
    "check_cross_source_consistency",
    "check_schema_regression",
    "check_signal_density",
    "compute_policy_fingerprint",
    "load_fingerprint_history",
    "load_schema_baseline",
    "run_strict_checks",
    "save_fingerprint_history",
    "update_fingerprint_history",
]
