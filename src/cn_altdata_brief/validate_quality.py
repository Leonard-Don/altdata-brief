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
import re
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
TEMPORAL_FLIP_RATE_MAX = 0.30  # day-over-day sign-flip ratio that triggers WARN
TEMPORAL_HISTORY_DAYS = 7  # rolling window for temporal_coherence_check
TEMPORAL_MIN_OBSERVATIONS = 3  # need ≥3 days before flip rate is meaningful

# Project root resolution (this file lives at src/cn_altdata_brief/).
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_FINGERPRINT_HISTORY = _PROJECT_ROOT / "output" / "fingerprint_history.json"
DEFAULT_SCHEMA_DIR = _PROJECT_ROOT / "tests" / "fixtures" / "schemas"
DEFAULT_SIGNAL_HISTORY = _PROJECT_ROOT / "output" / "signal_history.json"

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
                    "impact": round(_safe_float(info.get("avg_impact")), 4),
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
                    "impact": round(_safe_float(row.get("avg_impact")), 4),
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


def _safe_float(value: Any, default: float = 0.0) -> float:
    """Best-effort numeric coercion used by validators.

    Upstream public summaries occasionally carry placeholders such as
    ``"N/A"``. Treat those as ``default`` so validation returns a structured
    CheckResult/JSON payload instead of crashing with ``ValueError``.
    """
    try:
        out = float(value if value is not None else default)
    except (TypeError, ValueError):
        return default
    return default if math.isnan(out) else out


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
# Required-upstream-path check (ERROR-level) — schema-drift early warning
# ---------------------------------------------------------------------------
#
# ``schema_regression`` checks the *internal, normalized* payload shape —
# i.e. the dict AFTER the adapter's _normalize_* functions ran. That is
# blind to the silent-degradation failure mode: when an upstream renames a
# deep nested field, ``dict.get`` returns None/{}, the normalizer emits
# zeros, and ``schema_regression`` still sees a structurally-fine internal
# payload. The brief ships all-zeros with no loud signal.
#
# This check closes that gap by validating the RAW upstream public-summary
# JSON against the exact deep nested paths the adapters are contract-coupled
# to. If ``providers.macro_hf.metals`` disappears upstream, this FAILS and
# names the path — instead of the brief silently substituting 0.
#
# Path syntax: dotted segments. A ``[]`` segment means "a non-empty dict or
# list here" (we don't assert specific child keys for the leaf collections —
# their per-row shape is the adapter's concern; we assert the *container*
# the adapter reaches into actually exists and is populated).

#: Per-source required nested paths in the RAW public-summary JSON. These
#: mirror the deep paths each adapter's ``_load_from_public_summary`` reads.
#: When an upstream renames one of these, the check FAILs loudly.
#:
#: ETF 512400 is intentionally absent: its ``liveSnapshot.json`` is a JS-app
#: artifact with a different (non-``providers``) shape, and the ETF
#: structural checks already live in ``validate.py``
#: (``etf_512400.required_source_health`` etc.). Adding it here would
#: duplicate that coverage.
REQUIRED_UPSTREAM_PATHS: dict[str, tuple[str, ...]] = {
    "super_pricing": (
        "providers",
        "providers.policy_radar",
        "providers.policy_radar.industry_signals",
        "providers.policy_radar.policy_count",
        "providers.macro_hf",
        "providers.macro_hf.metals",
    ),
    "quant_trading": (
        "providers",
        "providers.policy_radar",
        "providers.policy_radar.policy_count",
        # The adapter prefers industry_heat.top_industries_by_score and
        # falls back to policy_radar.top_industries — at least one heat
        # source must exist; that "any-of" rule is handled specially below.
    ),
    "index_research": (
        "verdicts",
    ),
}

#: Required upstream paths that are intentionally scalar values. All other
#: required paths are adapter-input containers and must resolve to a non-empty
#: dict/list, not merely a truthy scalar placeholder.
REQUIRED_UPSTREAM_SCALAR_PATHS: dict[str, tuple[str, ...]] = {
    "super_pricing": ("providers.policy_radar.policy_count",),
    "quant_trading": ("providers.policy_radar.policy_count",),
}

#: Scalar upstream paths that must be accepted by the adapter's ``int()``
#: normalization, rather than merely being non-container placeholders.
REQUIRED_UPSTREAM_INT_SCALAR_PATHS: dict[str, tuple[str, ...]] = {
    "super_pricing": ("providers.policy_radar.policy_count",),
    "quant_trading": ("providers.policy_radar.policy_count",),
}

#: "any-of" path groups: at least ONE path in the tuple must resolve.
#: Used where the adapter has a documented fallback chain (quant-trading's
#: heat ranking can come from either provider block).
REQUIRED_UPSTREAM_ANY_OF: dict[str, tuple[tuple[str, ...], ...]] = {
    "quant_trading": (
        (
            "providers.industry_heat.top_industries_by_score",
            "providers.policy_radar.top_industries",
            "providers.policy_radar.industry_signals",
        ),
    ),
}


_MISSING = object()


def _resolve_nested_path(doc: Any, dotted: str) -> Any:
    """Walk a dotted path through nested dicts; return ``_MISSING`` if absent.

    Only dict traversal is supported — every segment must index into a
    ``dict``. A segment that hits a non-dict (or a missing key) yields
    ``_MISSING`` so the caller can report the exact failing path.
    """
    node: Any = doc
    for segment in dotted.split("."):
        if not isinstance(node, dict) or segment not in node:
            return _MISSING
        node = node[segment]
    return node


def _path_is_populated(value: Any) -> bool:
    """True when a resolved path holds a usable value.

    A required path that resolves to ``None``, an empty dict, or an empty
    list is treated as effectively-missing — an upstream that renamed the
    real field often leaves an empty husk behind. Scalars (ints, strings,
    bools, floats) count as populated.
    """
    if value is _MISSING or value is None:
        return False
    if isinstance(value, (dict, list, str)):
        return len(value) > 0
    return True


def _path_has_valid_type(value: Any, *, require_container: bool) -> bool:
    """True when a resolved path has the value kind the adapter can consume."""
    if value is _MISSING or value is None:
        return False
    if require_container:
        return isinstance(value, (dict, list))
    return not isinstance(value, (dict, list))


def _path_is_int_coercible_scalar(value: Any) -> bool:
    """True when a required count scalar can pass int() without going negative."""
    if isinstance(value, bool):
        return False
    try:
        coerced = int(value)
    except (TypeError, ValueError, OverflowError):
        return False
    return coerced >= 0


def _raw_summary_path_for(
    source_key: str, payload: AdapterPayload | None
) -> Path | None:
    """Find the raw public-summary file for a source.

    Prefers the ``public_summary_path`` the adapter stamped on its payload
    so the check reads the *exact* file the adapter consumed. Returns
    ``None`` when the payload did not come from an explicit public summary.
    A stamped path is returned even if it no longer exists, so the caller can
    surface the disappeared audit artifact as WARN instead of silently
    skipping it.
    """
    if payload is not None:
        stamped = payload.data.get("public_summary_path")
        if isinstance(stamped, str) and stamped:
            return Path(stamped)
    return None


def check_required_paths(
    payloads: dict[str, AdapterPayload | None],
    *,
    summary_paths: dict[str, Path] | None = None,
) -> CheckResult:
    """FAIL when a required deep nested path is missing from a raw summary.

    For each source in :data:`REQUIRED_UPSTREAM_PATHS`, the check locates
    the raw public-summary JSON, parses it, and verifies every required
    dotted path resolves to a populated value. Any missing path is
    surfaced **by name** — turning the previously-silent "upstream renamed
    a field → brief publishes zeros" failure into a loud, explicit FAIL.

    ``summary_paths`` lets tests/contract-tests inject the exact files to
    audit. In production the paths come from each payload's stamped
    ``public_summary_path``.

    Severity:

    * **FAIL** — at least one required path is missing/empty in a summary
      that was found and parsed.
    * **WARN** — a summary file could not be opened or parsed.
    * **INFO** — no auditable summary files found (e.g. every adapter ran
      in cache mode), or every required path resolved.
    """
    name = "required_paths"
    per_source: dict[str, dict[str, Any]] = {}
    missing_report: list[str] = []
    parse_warnings: list[str] = []
    audited = 0

    for source_key, required in REQUIRED_UPSTREAM_PATHS.items():
        if summary_paths is not None:
            explicit_path = summary_paths.get(source_key)
            path = Path(explicit_path) if explicit_path is not None else None
        else:
            path = _raw_summary_path_for(source_key, payloads.get(source_key))

        entry: dict[str, Any] = {"path": str(path) if path else None}
        if path is None:
            entry["status"] = "skipped_no_summary"
            per_source[source_key] = entry
            continue

        try:
            with Path(path).open(encoding="utf-8") as fh:
                doc = json.load(fh)
        except (OSError, json.JSONDecodeError) as exc:
            entry["status"] = "parse_error"
            entry["error"] = str(exc)
            parse_warnings.append(f"{source_key}: unreadable summary ({path})")
            per_source[source_key] = entry
            continue

        audited += 1
        source_missing: list[str] = []
        invalid_type_paths: dict[str, str] = {}
        scalar_paths = set(REQUIRED_UPSTREAM_SCALAR_PATHS.get(source_key, ()))
        int_scalar_paths = set(
            REQUIRED_UPSTREAM_INT_SCALAR_PATHS.get(source_key, ())
        )
        for dotted in required:
            value = _resolve_nested_path(doc, dotted)
            require_container = dotted not in scalar_paths
            if not _path_has_valid_type(value, require_container=require_container):
                if value is not _MISSING and value is not None:
                    invalid_type_paths[dotted] = type(value).__name__
                source_missing.append(dotted)
                continue
            if (
                dotted in int_scalar_paths
                and not _path_is_int_coercible_scalar(value)
            ):
                invalid_type_paths[dotted] = type(value).__name__
                source_missing.append(dotted)
                continue
            if not _path_is_populated(value):
                source_missing.append(dotted)

        # "any-of" groups: the group fails only when ALL of its members
        # are missing — that is a real schema drift the adapter can't
        # absorb via its documented fallback chain.
        for group in REQUIRED_UPSTREAM_ANY_OF.get(source_key, ()):
            for dotted in group:
                value = _resolve_nested_path(doc, dotted)
                if not _path_has_valid_type(value, require_container=True):
                    if value is not _MISSING and value is not None:
                        invalid_type_paths[dotted] = type(value).__name__
            if not any(
                _path_has_valid_type(
                    (value := _resolve_nested_path(doc, dotted)),
                    require_container=True,
                )
                and _path_is_populated(value)
                for dotted in group
            ):
                source_missing.append(" | ".join(group) + " (none present)")

        entry["status"] = "ok" if not source_missing else "missing_paths"
        entry["missing_paths"] = source_missing
        entry["invalid_type_paths"] = invalid_type_paths
        per_source[source_key] = entry
        if source_missing:
            shown = ", ".join(source_missing[:3])
            extra = (
                f" (+ {len(source_missing) - 3} more)"
                if len(source_missing) > 3
                else ""
            )
            missing_report.append(f"{source_key} missing: {shown}{extra}")

    detail = {
        "audited_sources": audited,
        "per_source": per_source,
    }

    if missing_report:
        warning_suffix = (
            f" Also unreadable summaries: {'; '.join(parse_warnings)}."
            if parse_warnings
            else ""
        )
        return CheckResult(
            name=name,
            level=FAIL,
            message=(
                "; ".join(missing_report)
                + ". Upstream schema drifted — the adapter would silently "
                "substitute zeros for these paths."
                + warning_suffix
            ),
            detail=detail,
        )
    if parse_warnings:
        return CheckResult(
            name=name,
            level=WARN,
            message="; ".join(parse_warnings),
            detail=detail,
        )
    if audited == 0:
        return CheckResult(
            name=name,
            level=INFO,
            message="no upstream public summaries available to audit; skipped.",
            detail=detail,
        )
    return CheckResult(
        name=name,
        level=INFO,
        message=(
            f"all required nested paths present across {audited} "
            "upstream public summaries."
        ),
        detail=detail,
    )


# ---------------------------------------------------------------------------
# Placeholder detector check (ERROR-level)
# ---------------------------------------------------------------------------
#
# The cross_source_consistency check caught a "测试行业" placeholder by
# accident once — the placeholder failed the *consistency* check because it
# didn't match the other source's industry list. A more direct guard is to
# scan published payload strings for known placeholder shapes and FAIL the
# pipeline before such content can ship. Severity is intentionally FAIL
# (ERROR) so a CI run with --strict --fail-on-warn blocks the publish step
# rather than emitting a soft warning.
#
# Pattern selection rationale:
#   * "测试" — exact 2-character token; "试" alone is a real CJK character
#     and appears in legitimate industry names ("试用期股", "试验机"). We
#     scan for "测试" verbatim so single-char "试" is unaffected.
#   * "示例" — same logic; "例" appears in real words. Two-char token only.
#   * English placeholders ("TODO", "FIXME", "PLACEHOLDER", "LOREM IPSUM")
#     — case-insensitive whole-word match so identifiers like "todomvc"
#     don't fire. We use a word-boundary regex.
#   * "占位" — Chinese for "placeholder". Two-char token, no word boundary.
#   * "test_" / "TEST_" prefix on slug-like strings.
#   * XXX / NNN sequences (3+ consecutive X or N caps) — placeholder
#     scaffolds emitted by templating tools.

# Patterns sorted in declaration order so reports are deterministic.
_PLACEHOLDER_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("cjk_test", re.compile(r"测试")),
    ("cjk_example", re.compile(r"示例")),
    ("cjk_placeholder", re.compile(r"占位")),
    ("en_todo", re.compile(r"\bTODO\b", re.IGNORECASE)),
    ("en_fixme", re.compile(r"\bFIXME\b", re.IGNORECASE)),
    ("en_placeholder", re.compile(r"\bplaceholder\b", re.IGNORECASE)),
    ("en_lorem_ipsum", re.compile(r"lorem\s+ipsum", re.IGNORECASE)),
    ("slug_test_prefix", re.compile(r"\btest_[A-Za-z0-9]")),
    ("scaffold_xxx", re.compile(r"X{3,}")),
    ("scaffold_nnn", re.compile(r"N{3,}")),
)

#: Path-segment substrings whose values are auxiliary metadata where placeholder
#: hits are expected (test profile names, schema versions, etc.). Without this
#: allow-list a legit ``active_profiles: ["e2e-smoke"]`` or ``"test_env"`` key
#: would FAIL the check. Match is substring-on-the-dotted-path so we can scope
#: by both key name and JSON path.
_PLACEHOLDER_PATH_ALLOWLIST: tuple[str, ...] = (
    # Quant ships a "paper_trading.active_profiles" list whose entries are
    # internal smoke-test profile names — they are intentionally test slugs.
    "paper_trading.active_profiles",
    # Schema-version and version strings are metadata, not user-facing content.
    "schema_version",
    "source_codebase_version",
    "baseline_version",
)


def _path_is_allowlisted(path: str) -> bool:
    """True iff ``path`` ends in or contains an allowlisted suffix.

    Paths look like ``providers.paper_trading.active_profiles[0]`` — we use
    substring containment so a wildcard list-index ``[0]`` doesn't escape
    the allowlist.
    """
    for needle in _PLACEHOLDER_PATH_ALLOWLIST:
        if needle in path:
            return True
    return False


def _iter_string_values(
    node: Any, path: str = ""
) -> Iterable[tuple[str, str]]:
    """Yield ``(json_path, string_value)`` for every string leaf in ``node``.

    The walk is depth-first; list indices appear as ``[i]`` so the surfaced
    path round-trips to a human reader. Non-string scalars are skipped —
    placeholders surface as text content, not as bools/ints.
    """
    if isinstance(node, str):
        yield (path, node)
        return
    if isinstance(node, dict):
        for key, value in node.items():
            child = f"{path}.{key}" if path else str(key)
            yield from _iter_string_values(value, child)
        return
    if isinstance(node, list):
        for idx, value in enumerate(node):
            yield from _iter_string_values(value, f"{path}[{idx}]")
        return
    # bool/int/float/None: nothing to scan.


def scan_payload_for_placeholders(
    payload: AdapterPayload | None,
) -> list[dict[str, str]]:
    """Return a list of placeholder hits for one payload.

    Each hit is ``{"path", "pattern", "value", "match"}``. The walk reports
    EVERY hit (not just the first) so the operator sees the full picture
    when more than one slipped through. Allowlisted paths are silently
    skipped — see :data:`_PLACEHOLDER_PATH_ALLOWLIST`.
    """
    if payload is None:
        return []
    hits: list[dict[str, str]] = []
    for path, value in _iter_string_values(payload.data):
        if _path_is_allowlisted(path):
            continue
        for pattern_name, regex in _PLACEHOLDER_PATTERNS:
            match = regex.search(value)
            if match is None:
                continue
            hits.append(
                {
                    "path": path,
                    "pattern": pattern_name,
                    "value": value,
                    "match": match.group(0),
                }
            )
    return hits


def check_placeholder_detector(
    payloads: dict[str, AdapterPayload | None],
) -> CheckResult:
    """FAIL if any payload string matches a known placeholder pattern.

    A direct content-scan complement to ``cross_source_consistency``,
    which only catches placeholders that happen to break cross-source
    agreement. Once a placeholder is detected, the verdict is **FAIL** —
    placeholders shipping to production is unacceptable, and the
    ``--strict --fail-on-warn`` pipeline blocks the subsequent publish.
    """
    name = "placeholder_detector"
    per_source: dict[str, list[dict[str, str]]] = {}
    summary_lines: list[str] = []
    for source_key, payload in payloads.items():
        hits = scan_payload_for_placeholders(payload)
        if hits:
            per_source[source_key] = hits
            first = hits[0]
            more = f" (+ {len(hits) - 1} more)" if len(hits) > 1 else ""
            summary_lines.append(
                f"{source_key} {first['path']} matched {first['pattern']} "
                f"('{first['match']}'){more}"
            )

    detail = {
        "patterns": [p[0] for p in _PLACEHOLDER_PATTERNS],
        "path_allowlist": list(_PLACEHOLDER_PATH_ALLOWLIST),
        "hits_per_source": per_source,
    }
    if per_source:
        return CheckResult(
            name=name,
            level=FAIL,
            message="; ".join(summary_lines),
            detail=detail,
        )
    return CheckResult(
        name=name,
        level=INFO,
        message=(
            f"no placeholder patterns detected across "
            f"{len(payloads)} sources ({len(_PLACEHOLDER_PATTERNS)} patterns scanned)."
        ),
        detail=detail,
    )


# ---------------------------------------------------------------------------
# Temporal coherence check (WARN-level)
# ---------------------------------------------------------------------------
#
# Same source should not flip directional signals day-over-day without an
# accompanying regime change flag. A "bullish → bearish → bullish" zigzag
# from one provider is either a data bug or a real regime flip; the
# operator should see the WARN and decide.
#
# History layout, persisted to ``output/signal_history.json``::
#
#     {
#       "super_pricing": {
#         "policy_radar.新能源汽车": [
#           {"date": "2026-05-17", "signal": "bullish"},
#           {"date": "2026-05-18", "signal": "bearish"},
#           ...
#         ],
#         ...
#       },
#       ...
#     }
#
# We cap each per-key list at TEMPORAL_HISTORY_DAYS entries.


@dataclass(slots=True)
class SignalObservation:
    """One day's directional reading of a tracked signal."""

    date: str  # YYYY-MM-DD
    signal: str  # "bullish" | "bearish" | "neutral" (lowercased)


def load_signal_history(
    path: Path,
) -> dict[str, dict[str, list[SignalObservation]]]:
    """Read the persisted per-source signal history; tolerate missing files."""
    if not path.exists():
        return {}
    try:
        with path.open(encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}
    out: dict[str, dict[str, list[SignalObservation]]] = {}
    if not isinstance(raw, dict):
        return out
    for source_key, signals in raw.items():
        if not isinstance(signals, dict):
            continue
        per_signal: dict[str, list[SignalObservation]] = {}
        for signal_key, entries in signals.items():
            if not isinstance(entries, list):
                continue
            loaded: list[SignalObservation] = []
            for row in entries:
                if not isinstance(row, dict):
                    continue
                date = str(row.get("date", "") or "")
                signal = str(row.get("signal", "") or "").strip().lower()
                if not date or not signal:
                    continue
                loaded.append(SignalObservation(date=date, signal=signal))
            if loaded:
                per_signal[str(signal_key)] = loaded
        if per_signal:
            out[str(source_key)] = per_signal
    return out


def save_signal_history(
    path: Path,
    history: dict[str, dict[str, list[SignalObservation]]],
    *,
    max_entries: int = TEMPORAL_HISTORY_DAYS,
) -> None:
    """Persist per-source signal history, trimming each list to ``max_entries``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    out: dict[str, dict[str, list[dict[str, str]]]] = {}
    for source_key, signals in history.items():
        per_signal: dict[str, list[dict[str, str]]] = {}
        for signal_key, entries in signals.items():
            trimmed = entries[-max_entries:] if len(entries) > max_entries else entries
            per_signal[signal_key] = [
                {"date": e.date, "signal": e.signal} for e in trimmed
            ]
        out[source_key] = per_signal
    with path.open("w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=2, sort_keys=True)
        fh.write("\n")


def _extract_signals_for_temporal(
    source_key: str, payload: AdapterPayload | None
) -> dict[str, str]:
    """Pull today's directional signals out of a payload, keyed by signal id.

    Mirrors the cross-source consistency adapters but emits a flat
    ``{signal_id: direction}`` map keyed by source-namespaced ids so they
    don't collide across sources in the on-disk history.
    """
    if payload is None:
        return {}
    out: dict[str, str] = {}
    if source_key == "super_pricing":
        rows = (payload.data.get("policy_radar") or {}).get(
            "industry_signals"
        ) or []
        if isinstance(rows, dict):
            iterable: Iterable[tuple[str, Any]] = list(rows.items())
            for name, info in iterable:
                if not isinstance(info, dict):
                    continue
                sig = str(info.get("signal", "")).strip().lower()
                if sig:
                    out[f"policy_radar.{name}"] = sig
        elif isinstance(rows, list):
            for row in rows:
                if not isinstance(row, dict):
                    continue
                name = str(row.get("industry", "")).strip()
                sig = str(row.get("signal", "")).strip().lower()
                if name and sig:
                    out[f"policy_radar.{name}"] = sig
        # Regime classifier (if present): emit one signal per regime field.
        regime = (payload.data.get("regime_classifier") or {})
        regime_name = str(regime.get("regime_name", "")).strip().lower()
        if regime_name:
            out["regime_classifier.regime_name"] = regime_name
    elif source_key == "quant_trading":
        rows = payload.data.get("industries") or []
        if isinstance(rows, list):
            for row in rows:
                if not isinstance(row, dict):
                    continue
                name = str(
                    row.get("industry") or row.get("industry_name") or ""
                ).strip()
                sig = str(row.get("policy_signal", "")).strip().lower()
                if name and sig:
                    out[f"industries.{name}"] = sig
        # Composite signal (etf_rotation regime recommendation if shipped).
        rot = (payload.data.get("etf_rotation") or {}).get(
            "regime_recommendation"
        ) or {}
        rec = str(rot.get("recommended_strategy", "")).strip().lower()
        if rec:
            out["etf_rotation.recommended_strategy"] = rec
    return out


def _is_regime_change_present(payload: AdapterPayload | None) -> bool:
    """Detect a "regime change" annotation that legitimizes a sign flip.

    Sources can opt into the regime-change opt-out by setting any of:

    * ``payload.data["regime_change_event"]`` truthy,
    * ``payload.data["regime_classifier"]["regime_change_event"]`` truthy,
    * ``payload.data["meta"]["regime_change_event"]`` truthy.

    The check is intentionally loose — operators can flag a real volatility
    event without coordinating on field placement.
    """
    if payload is None:
        return False
    candidates = (
        payload.data.get("regime_change_event"),
        (payload.data.get("regime_classifier") or {}).get("regime_change_event"),
        (payload.data.get("meta") or {}).get("regime_change_event"),
    )
    return any(bool(c) for c in candidates)


def _sign_flips(observations: list[SignalObservation]) -> tuple[int, int]:
    """Compute ``(flip_count, transition_count)`` over a series.

    A "sign flip" is a transition between non-neutral opposite directions
    (``bullish ↔ bearish``). Transitions involving ``neutral`` are counted
    as transitions but not flips — a clean ramp through neutral isn't
    considered jittery.
    """
    if len(observations) < 2:
        return (0, 0)
    flips = 0
    transitions = 0
    # ``observations`` vs ``observations[1:]`` is intentionally pairwise on
    # consecutive elements — strict zip would reject the length mismatch.
    for prev, curr in zip(observations[:-1], observations[1:], strict=True):
        transitions += 1
        if (prev.signal == "bullish" and curr.signal == "bearish") or (
            prev.signal == "bearish" and curr.signal == "bullish"
        ):
            flips += 1
    return (flips, transitions)


def _append_observation(
    history: dict[str, dict[str, list[SignalObservation]]],
    *,
    source_key: str,
    signal_key: str,
    today: str,
    signal: str,
) -> list[SignalObservation]:
    """Append today's observation, replacing same-day record if present."""
    per_source = history.setdefault(source_key, {})
    series = per_source.setdefault(signal_key, [])
    if series and series[-1].date == today:
        series[-1] = SignalObservation(date=today, signal=signal)
    else:
        series.append(SignalObservation(date=today, signal=signal))
    return series


def check_temporal_coherence(
    payloads: dict[str, AdapterPayload | None],
    *,
    history_path: Path | None = None,
    today: str | None = None,
    persist: bool = True,
    flip_rate_max: float = TEMPORAL_FLIP_RATE_MAX,
    min_observations: int = TEMPORAL_MIN_OBSERVATIONS,
    history_days: int = TEMPORAL_HISTORY_DAYS,
) -> CheckResult:
    """WARN when a source's daily signals zigzag without a regime-change flag.

    For each source, append today's signals to the rolling history file
    and compute the per-signal day-over-day sign-flip rate. If any signal
    exceeds ``flip_rate_max`` AND the source's payload carries no
    ``regime_change_event`` annotation, emit WARN. The check intentionally
    stays WARN (not FAIL) — a real volatility event can produce a high
    flip rate, and the operator's eye is the final filter.

    Empty / missing payloads gracefully skip — there is no time series to
    score, so the check returns INFO.
    """
    name = "temporal_coherence"
    history_path = history_path if history_path else DEFAULT_SIGNAL_HISTORY
    today = today or datetime.now(UTC).strftime("%Y-%m-%d")

    history = load_signal_history(history_path)
    per_source: dict[str, dict[str, Any]] = {}
    jittery: list[dict[str, Any]] = []
    any_observation_recorded = False

    for source_key, payload in payloads.items():
        regime_change = _is_regime_change_present(payload)
        signals_today = _extract_signals_for_temporal(source_key, payload)
        per_signal_report: dict[str, dict[str, Any]] = {}
        if not signals_today:
            per_source[source_key] = {
                "status": "no_signals",
                "regime_change_event": regime_change,
                "signals": per_signal_report,
            }
            continue
        any_observation_recorded = True
        for signal_key, direction in signals_today.items():
            series = _append_observation(
                history,
                source_key=source_key,
                signal_key=signal_key,
                today=today,
                signal=direction,
            )
            # Keep only the most recent N observations for flip-rate math.
            window = series[-history_days:] if len(series) > history_days else series
            flips, transitions = _sign_flips(window)
            flip_rate = (flips / transitions) if transitions > 0 else 0.0
            entry: dict[str, Any] = {
                "observations": len(window),
                "flips": flips,
                "transitions": transitions,
                "flip_rate": round(flip_rate, 3),
                "today": direction,
            }
            per_signal_report[signal_key] = entry
            if len(window) >= min_observations and flip_rate > flip_rate_max:
                # WARN candidate. Suppress if the source declared a regime
                # change for the day — that legitimizes the flip.
                if not regime_change:
                    jittery.append(
                        {
                            "source": source_key,
                            "signal": signal_key,
                            "flip_rate": entry["flip_rate"],
                            "flips": flips,
                            "transitions": transitions,
                            "observations": len(window),
                        }
                    )
        per_source[source_key] = {
            "status": "scored",
            "regime_change_event": regime_change,
            "signals": per_signal_report,
        }

    if persist and any_observation_recorded:
        save_signal_history(history_path, history, max_entries=history_days)

    detail = {
        "history_path": str(history_path),
        "today": today,
        "flip_rate_max": flip_rate_max,
        "history_days": history_days,
        "min_observations": min_observations,
        "per_source": per_source,
        "jittery": jittery,
    }

    if jittery:
        first = jittery[0]
        more = f" (+ {len(jittery) - 1} more)" if len(jittery) > 1 else ""
        return CheckResult(
            name=name,
            level=WARN,
            message=(
                f"{first['source']}.{first['signal']} flipped "
                f"{first['flips']}/{first['transitions']} transitions "
                f"({first['flip_rate']:.0%} > {flip_rate_max:.0%}); "
                f"no regime_change_event{more}"
            ),
            detail=detail,
        )
    if not any_observation_recorded:
        return CheckResult(
            name=name,
            level=INFO,
            message="no time-series signals available; temporal_coherence skipped.",
            detail=detail,
        )
    return CheckResult(
        name=name,
        level=INFO,
        message=(
            f"day-over-day signal flips below {flip_rate_max:.0%} across "
            f"{sum(len(s['signals']) for s in per_source.values())} signals."
        ),
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
    signal_history_path: Path | None = None,
    summary_paths: dict[str, Path] | None = None,
) -> list[CheckResult]:
    """Run any subset of the seven quality checks.

    ``include`` is a tuple of check identifiers — when ``None``, runs all
    seven. Identifiers map to: ``"fingerprint"``, ``"density"``,
    ``"consistency"``, ``"schema"``, ``"placeholder"``, ``"temporal"``,
    ``"required_paths"``.

    ``required_paths`` (v0.13) audits the RAW upstream public-summary JSON
    for the deep nested paths the adapters depend on — the loud-failure
    guard against silent schema drift.
    """
    include = include if include is not None else (
        "fingerprint",
        "density",
        "consistency",
        "schema",
        "placeholder",
        "temporal",
        "required_paths",
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
    if "placeholder" in include:
        out.append(check_placeholder_detector(payloads))
    if "temporal" in include:
        out.append(
            check_temporal_coherence(
                payloads,
                history_path=signal_history_path,
                today=today,
                persist=persist,
            )
        )
    if "required_paths" in include:
        out.append(check_required_paths(payloads, summary_paths=summary_paths))
    return out


__all__ = [
    "DEFAULT_FINGERPRINT_HISTORY",
    "DEFAULT_SCHEMA_DIR",
    "DEFAULT_SIGNAL_HISTORY",
    "FINGERPRINT_STALE_DAYS",
    "FingerprintEntry",
    "POLICY_IMPACT_FLOOR",
    "REQUIRED_UPSTREAM_ANY_OF",
    "REQUIRED_UPSTREAM_PATHS",
    "SIGNAL_DENSITY_MIN_RATIO",
    "SignalObservation",
    "TEMPORAL_FLIP_RATE_MAX",
    "TEMPORAL_HISTORY_DAYS",
    "TEMPORAL_MIN_OBSERVATIONS",
    "check_content_fingerprint_freshness",
    "check_cross_source_consistency",
    "check_placeholder_detector",
    "check_required_paths",
    "check_schema_regression",
    "check_signal_density",
    "check_temporal_coherence",
    "compute_policy_fingerprint",
    "load_fingerprint_history",
    "load_schema_baseline",
    "load_signal_history",
    "run_strict_checks",
    "save_fingerprint_history",
    "save_signal_history",
    "scan_payload_for_placeholders",
    "update_fingerprint_history",
]
