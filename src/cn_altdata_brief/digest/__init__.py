"""v0.9 — weekly digest module.

Aggregates a workweek's worth of daily briefs (Mon-Fri) into a single
``本周回顾`` markdown document. The synthesis is **purely deterministic**:
we parse the already-written CN brief markdown files, extract structured
per-day signals via small regexes (no LLM in v0.9 core — an optional
``--with-llm`` flag reuses the v0.8 translator to produce an EN sibling
of the weekly digest).

Design contract
---------------

* Reads ``output/briefs/YYYY-MM-DD.md`` files; never re-fetches the
  underlying adapter caches. The daily brief IS the persistence layer.
* Tolerates missing days — an empty week or a single-brief week still
  produces a digest with a "fewer-than-5-briefs" note in the body.
* The aggregation is "themes appearing 3+ days" (industries / metals)
  + "inflections" (signals that flipped sign during the week).
* The deferred-cost cycle of `compose -> render -> write` is mirror-of
  the daily pipeline so callers familiar with v0.2+ will recognize it.
"""

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
    "DEFAULT_RECURRENCE_THRESHOLD",
    "DailyBriefSummary",
    "Inflection",
    "Theme",
    "WeeklyDigest",
    "collect_brief_paths_for_week",
    "compose_weekly_digest",
    "iso_week_bounds",
    "parse_brief",
]
