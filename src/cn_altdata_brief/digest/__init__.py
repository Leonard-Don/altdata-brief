"""v0.9 — weekly digest module; v0.11 — monthly digest module.

Aggregates daily briefs into longer-horizon digests:

* **Weekly** (v0.9) — Mon-Fri workweek aggregated into a single
  ``本周回顾`` markdown document. Themes recur ≥3 days; signal
  inflections fire when a sign flips mid-week.
* **Monthly** (v0.11) — calendar month aggregated into ``上月回顾``.
  Sustained themes appear ≥12 days; reversal events count every
  within-month flip; ETF NAV month-over-month change is decomposed
  into first/last/high/low.

Design contract
---------------

* Reads ``output/briefs/YYYY-MM-DD.md`` files (and ``output/digests/
  <iso_year>-W<week>.md`` for the monthly tier); never re-fetches the
  underlying adapter caches. The brief IS the persistence layer.
* Tolerates missing days — empty windows still produce a digest with
  a degradation ``note`` in the body.
* Aggregation is fully deterministic; LLM is only used (optionally)
  for the EN translation sibling via v0.8 infrastructure.
"""

from cn_altdata_brief.digest.monthly import (
    CARRY_FORWARD_LAST_WEEK_THRESHOLD,
    DEFAULT_SUSTAINED_THRESHOLD,
    ETFMonthlySummary,
    MonthlyDigest,
    ReversalEvent,
    SustainedTheme,
    WeeklyDigestSummary,
    collect_brief_paths_for_month,
    collect_digest_paths_for_month,
    compose_monthly_digest,
    is_business_day,
    month_bounds,
    parse_weekly_digest,
    previous_month,
)
from cn_altdata_brief.digest.weekly import (
    DEFAULT_RECURRENCE_THRESHOLD,
    DailyBriefSummary,
    Inflection,
    Theme,
    WeeklyDigest,
    collect_brief_paths_for_week,
    compose_weekly_digest,
    iso_week_bounds,
    parse_brief,
)

__all__ = [
    "CARRY_FORWARD_LAST_WEEK_THRESHOLD",
    "DEFAULT_RECURRENCE_THRESHOLD",
    "DEFAULT_SUSTAINED_THRESHOLD",
    "DailyBriefSummary",
    "ETFMonthlySummary",
    "Inflection",
    "MonthlyDigest",
    "ReversalEvent",
    "SustainedTheme",
    "Theme",
    "WeeklyDigest",
    "WeeklyDigestSummary",
    "collect_brief_paths_for_month",
    "collect_brief_paths_for_week",
    "collect_digest_paths_for_month",
    "compose_monthly_digest",
    "compose_weekly_digest",
    "is_business_day",
    "iso_week_bounds",
    "month_bounds",
    "parse_brief",
    "parse_weekly_digest",
    "previous_month",
]
