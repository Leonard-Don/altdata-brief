"""Mock baseline values used by the observation synthesizer (v0.2).

A *real* 7-day rolling baseline will be wired in v0.3 by reading the
super-pricing narrative archive directly. For now the values below are
hand-tuned constants derived from eyeballing the last week of caches,
so the observation sentences feel anchored rather than rule-blind.

When the real archive wiring lands, this module's only job becomes
fallback: if ``load_recent_history()`` returns ``None`` the synthesizer
falls back to these constants instead of producing an ungrounded line.
"""

from __future__ import annotations

from typing import Any

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


def load_recent_history() -> dict[str, Any] | None:
    """Future hook: read the last 7 entries from super-pricing's archive.

    Returns None today (v0.2). v0.3 will return::

        {
          "policy_impact_7d": [...],
          "metal_spread_7d": [...],
          "etf_nav_7d": [...],
          "industry_heat_7d": [...],
        }

    The observation module already handles ``None`` by falling back to
    the module-level constants above, so callers don't need to special-case.
    """
    return None


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
