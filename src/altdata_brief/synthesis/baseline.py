"""Rolling 7-day baselines for the observation synthesizer.

v0.2 anchored the "对比近 7 日" context lines on hand-tuned constants.
v0.3 wires the **policy-impact** baseline to super-pricing's *real*
narrative archive (``cache/alt_data/narrative_history.jsonl``):
:func:`load_recent_history` reads the recent entries and
:func:`resolve_baseline` reduces a metric's series to its mean — falling back
to the module-level constants below when the archive is absent (CI, fresh
clones) or carries no usable signal.

Only ``policy_impact_7d`` is archive-backed today: the narrative archive
records the policy-radar top-industry ``avg_impact`` (embedded in the entry's
prose) but does **not** expose per-day metal-spread / ETF-NAV / industry-heat
numerics, so those three baselines remain constant-fallback until
super-pricing publishes them. The per-metric fallback in
:func:`resolve_baseline` makes adding one later a one-line change: return the
new key from :func:`load_recent_history` and the observation layer picks it up
automatically.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from altdata_brief.config import narrative_history_path

# Rolling 7-day average of |avg_impact| for the policy-radar top industry.
# Empirically ~0.18 over the last week of super-pricing snapshots.
BASELINE_POLICY_IMPACT_7D = 0.18

# Rolling 7-day max absolute price-change-pct across all tracked metals.
# The macro_hf cache typically swings in the 0.6–1.5% range.
BASELINE_METAL_SPREAD_7D = 0.95

# Rolling 7-day average |daily_return| for ETF 512400 NAV (~0.6%).
BASELINE_ETF_NAV_VOL_7D = 0.006

# Rolling 7-day average heat score for the #1 industry.
BASELINE_INDUSTRY_HEAT_7D = 0.65

# How many trading days a signal must persist to be deemed "actionable".
SIGNAL_PERSISTENCE_DAYS = 3

#: How many recent signal-bearing entries form the rolling window.
_DEFAULT_HISTORY_LIMIT = 7

#: The policy-radar ``avg_impact`` is stored inside the narrative prose, e.g.
#: ``"... 行业影响力 avg_impact=-0.35, 偏空。"`` — pull the number out.
_AVG_IMPACT_RE = re.compile(r"avg_impact\s*=\s*([-+]?\d+(?:\.\d+)?)")


def _extract_avg_impact(entry: dict[str, Any]) -> float | None:
    """Pull the policy-radar ``avg_impact`` magnitude from a narrative entry.

    The figure lives in the human-readable ``summary``/``bullets`` text rather
    than a structured field, so we regex it out and return its absolute value
    (the baseline is the mean of ``|avg_impact|``). Returns ``None`` when no
    figure is present — e.g. the industry-less filler entries the archive
    interleaves — so the caller can skip the entry.
    """
    haystack = str(entry.get("summary") or "")
    bullets = entry.get("bullets")
    if isinstance(bullets, list):
        haystack = haystack + " " + " ".join(str(b) for b in bullets)
    match = _AVG_IMPACT_RE.search(haystack)
    if match is None:
        return None
    try:
        return abs(float(match.group(1)))
    except (TypeError, ValueError):  # pragma: no cover - regex guarantees float shape
        return None


def load_recent_history(
    path: Path | None = None, *, limit: int = _DEFAULT_HISTORY_LIMIT
) -> dict[str, Any] | None:
    """Read the recent policy-impact series from super-pricing's archive.

    Reads up to the last ``limit`` *signal-bearing* entries (those carrying a
    parseable ``avg_impact``) from ``narrative_history.jsonl`` and returns::

        {"policy_impact_7d": [0.35, 0.12, ...]}  # |avg_impact|, oldest→newest

    Returns ``None`` when the archive is missing/unreadable or contains no
    parseable entry — the observation module then falls back to the
    module-level constants. Malformed JSONL lines are skipped, not fatal.

    The archive interleaves multiple entries per refresh (and industry-less
    filler), so this is "last ``limit`` *signal* entries", not "last ``limit``
    calendar days". Only ``policy_impact_7d`` is populated today (see module
    docstring). ``path`` is overridable for tests; it defaults to
    :func:`~altdata_brief.config.narrative_history_path`.
    """
    history_path = Path(path) if path is not None else narrative_history_path()
    try:
        raw = history_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None

    impacts: list[float] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(entry, dict):
            continue
        value = _extract_avg_impact(entry)
        if value is not None:
            impacts.append(value)

    if not impacts:
        return None
    return {"policy_impact_7d": impacts[-limit:]}


def resolve_baseline(history: dict[str, Any] | None, key: str, fallback: float) -> float:
    """Reduce a metric's recent series to its mean, else return ``fallback``.

    ``history`` is the dict from :func:`load_recent_history` (or ``None``).
    When ``history[key]`` is a non-empty list of numbers, returns their mean;
    otherwise returns ``fallback``. This is the single fallback seam every
    observation baseline flows through, so a missing/empty archive degrades
    cleanly to the hand-tuned constants.
    """
    if not history:
        return fallback
    series = history.get(key)
    if not isinstance(series, list) or not series:
        return fallback
    values = [float(v) for v in series if isinstance(v, (int, float)) and not isinstance(v, bool)]
    if not values:
        return fallback
    return sum(values) / len(values)


def describe_intensity(value: float, baseline: float) -> str:
    """Return a one-word Chinese descriptor of value vs. baseline.

    >>> describe_intensity(0.4, 0.18)
    '显著走强'
    >>> describe_intensity(0.18, 0.18)
    '与近期持平'
    """
    if baseline <= 0:
        return "无可比基线"
    ratio = abs(value) / baseline
    if ratio >= 2.0:
        return "显著走强"
    if ratio >= 1.3:
        return "高于近周均值"
    if ratio >= 0.7:
        return "与近期持平"
    if ratio >= 0.3:
        return "弱于近周均值"
    return "明显衰减"
