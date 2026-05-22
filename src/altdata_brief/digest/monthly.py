"""v0.11 — deterministic monthly digest synthesis.

The monthly digest is the third tier of the cadence trilogy. Where the
daily brief says "what happened today across 4 alt-data sources?" and
the weekly digest says "which themes recurred this week?", the monthly
digest answers "across an entire month of ~20 dailies + 4 weeklies,
which signals **sustained** through multiple weeks, and which **flipped
across week boundaries**?".

Parsing strategy
----------------

The monthly aggregator reads two input sets:

1. **Daily briefs** for every workday in the month
   (``output/briefs/YYYY-MM-DD.md``). Each is parsed via the v0.9
   :func:`parse_brief` so the contract with the deterministic daily
   template stays single-source.
2. **Weekly digests** for the ISO weeks that intersect the month
   (``output/digests/<iso_year>-W<week>.md``). They are parsed via a
   lightweight regex that extracts the week's themes, inflections,
   and ETF netflow line.

Aggregation rules (v0.11, deterministic)
----------------------------------------

* **Sustained theme** = industry / metal that appears in the daily
  briefs on ≥``DEFAULT_SUSTAINED_THRESHOLD`` (default = 12) distinct
  trading days of the month. This is the "12+ days" rule from the spec
  and is the multi-week analog of the weekly digest's 3-day theme.
* **Reversal event** = (name, kind) whose sign flipped at least once
  during the month. We re-run the v0.9 inflection detector across the
  full month-length window so a flip on day-15 still counts even if
  the surrounding week digests don't mention it. Each event also
  carries the per-week sign trajectory so the renderer can show
  ``W18: +1 · W19: +1 · W20: -1``.
* **Cumulative impact** = sum of per-day ``avg_impact`` per industry
  across the month — same primitive as the weekly aggregation, just
  with a longer window. Sorted by ``|cumulative|`` and capped at the
  top 10 in the renderer.
* **ETF month-over-month change** = ETF NAV % move on the first vs
  last available trading day of the month, plus intramonth high / low
  (max and min daily return, with their dates).
* **Carry-forward forecast** = sustained themes that persisted into
  the last week of the month (their final-week occurrence count is
  ≥3). They appear in a deterministic "下月观察" bullet list — this is
  **not a prediction**; it is "these signals were still alive at
  month-end, watch them next month".

No external data fetches happen inside ``compose_monthly_digest`` —
the function is a pure transform of file paths to a ``MonthlyDigest``
dataclass. Same contract as the v0.9 weekly aggregator.
"""

from __future__ import annotations

import calendar
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from altdata_brief.digest.weekly import (
    DailyBriefSummary,
    Inflection,
    _detect_inflections,
    iso_week_bounds,
    parse_brief,
)

logger = logging.getLogger(__name__)

# Number of distinct trading days an industry must appear on before
# it qualifies as a "sustained" monthly theme. v0.11 default is 12
# (roughly 60% of a typical ~20-trading-day month). Tests use this
# constant directly so changes are auditable.
DEFAULT_SUSTAINED_THRESHOLD = 12

# Per-week occurrence count required to flag a theme as "carrying
# forward" into next month. A sustained theme that only ran in W1 of
# the month is not really "carrying" — we want continued activity in
# the final week.
CARRY_FORWARD_LAST_WEEK_THRESHOLD = 3

# Regex that extracts ``2026-W20`` style weekly digest stems from
# filenames inside ``output/digests/``.
_WEEKLY_DIGEST_STEM_RE = re.compile(r"^(?P<year>\d{4})-W(?P<week>\d{2})$")

# Sparse regexes that read the rendered weekly digest markdown. We
# only need a few fields (week label, ETF cumulative, theme count) for
# the constituents table — the digests_aggregated section quotes the
# already-rendered file rather than re-running the synthesis.
_WEEKLY_HEADER_RE = re.compile(
    r"^# 本周回顾 W(?P<week>\d{2})\s+—\s+(?P<start>\d{4}-\d{2}-\d{2})\s+→\s+(?P<end>\d{4}-\d{2}-\d{2})",
    flags=re.MULTILINE,
)
_WEEKLY_ETF_RE = re.compile(
    r"ETF\s+512400\s+周累计\s+NAV\s+变动\s+`(?P<pct>[+-]?\d+(?:\.\d+)?)%`"
)
_WEEKLY_THEME_COUNT_RE = re.compile(r"themes_count:\s+(?P<n>\d+)")
_WEEKLY_INFLECTION_COUNT_RE = re.compile(r"inflections_count:\s+(?P<n>\d+)")


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class WeeklyDigestSummary:
    """Lightweight handle on an already-written weekly digest file.

    The monthly digest doesn't re-aggregate from raw briefs through the
    weekly layer; it reads dailies directly. But it still wants to
    reference the constituent weekly digests in the footer (link, week
    number, headline). This summary holds exactly what the footer
    table needs.
    """

    path: Path
    iso_year: int
    iso_week: int
    week_start: date | None
    week_end: date | None
    etf_weekly_pct: float | None
    themes_count: int
    inflections_count: int

    @property
    def stem(self) -> str:
        return f"{self.iso_year}-W{self.iso_week:02d}"


@dataclass(slots=True)
class SustainedTheme:
    """An industry / metal that ran ≥N trading days across the month.

    Carries the **per-week occurrence count** (how many days inside
    each ISO week the theme appeared) so the renderer can show a
    week-by-week journey.
    """

    name: str
    kind: str  # 'policy' or 'inventory'
    occurrence_days: int
    per_week: list[tuple[str, int]]  # [(week_label e.g. 'W18', days_in_week), ...]
    cumulative_impact: float
    label_hint: str | None = None
    last_week_occurrences: int = 0


@dataclass(slots=True)
class ReversalEvent:
    """A within-month signal sign flip.

    The same dataclass shape as the weekly :class:`Inflection`, with
    one extra field: ``flips_in_month`` counts every sign change in
    the series (not just the last transition) so a multi-flip name
    surfaces with the higher count and the renderer can sort by it.
    """

    name: str
    kind: str  # 'policy' or 'inventory'
    flipped_from: int
    flipped_to: int
    flip_dates: list[str]
    flips_in_month: int


@dataclass(slots=True)
class ETFMonthlySummary:
    """ETF 512400 month-over-month roll-up."""

    first_day: str | None  # date_iso of first daily with data
    last_day: str | None
    first_day_pct: float | None
    last_day_pct: float | None
    month_cumulative_pct: float | None
    high_day: str | None
    high_pct: float | None
    low_day: str | None
    low_pct: float | None


@dataclass(slots=True)
class MonthlyDigest:
    """The full deterministic monthly digest payload.

    ``digests_aggregated`` lets the renderer link back to the
    constituent weekly digests; ``briefs_aggregated`` keeps the same
    role for daily briefs.
    """

    month_start: date
    month_end: date
    month_label: str  # e.g. '2026-04'
    briefs_aggregated: list[DailyBriefSummary] = field(default_factory=list)
    digests_aggregated: list[WeeklyDigestSummary] = field(default_factory=list)
    sustained_themes: list[SustainedTheme] = field(default_factory=list)
    reversal_events: list[ReversalEvent] = field(default_factory=list)
    industry_cumulative_impact: dict[str, float] = field(default_factory=dict)
    etf_monthly_summary: ETFMonthlySummary = field(
        default_factory=lambda: ETFMonthlySummary(
            first_day=None,
            last_day=None,
            first_day_pct=None,
            last_day_pct=None,
            month_cumulative_pct=None,
            high_day=None,
            high_pct=None,
            low_day=None,
            low_pct=None,
        )
    )
    top_signals: list[str] = field(default_factory=list)
    forecast_bullets: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    fetched_at: str = ""

    @property
    def month_start_iso(self) -> str:
        return self.month_start.isoformat()

    @property
    def month_end_iso(self) -> str:
        return self.month_end.isoformat()

    @property
    def brief_count(self) -> int:
        """Count of dailies that contributed at least one signal."""
        return sum(1 for b in self.briefs_aggregated if not b.is_empty)

    @property
    def digest_count(self) -> int:
        return len(self.digests_aggregated)

    def render_context(self) -> dict[str, Any]:
        """Frozen dict consumed by ``templates/monthly_digest.md.j2``."""
        return {
            "month_label": self.month_label,
            "month_start": self.month_start_iso,
            "month_end": self.month_end_iso,
            "fetched_at": self.fetched_at,
            "brief_count": self.brief_count,
            "digest_count": self.digest_count,
            "briefs_aggregated": [
                {
                    "date": b.date_iso,
                    "filename": b.path.name,
                    "is_empty": b.is_empty,
                    "policy_count": len(b.policy_signals),
                    "inventory_count": len(b.inventory_signals),
                    "etf_daily_pct": b.etf_daily_return_pct,
                }
                for b in self.briefs_aggregated
            ],
            "digests_aggregated": [
                {
                    "stem": d.stem,
                    "filename": d.path.name,
                    "iso_year": d.iso_year,
                    "iso_week": d.iso_week,
                    "week_start": d.week_start.isoformat() if d.week_start else None,
                    "week_end": d.week_end.isoformat() if d.week_end else None,
                    "etf_weekly_pct": d.etf_weekly_pct,
                    "themes_count": d.themes_count,
                    "inflections_count": d.inflections_count,
                }
                for d in self.digests_aggregated
            ],
            "sustained_themes": [
                {
                    "name": t.name,
                    "kind": t.kind,
                    "occurrence_days": t.occurrence_days,
                    "cumulative_impact": t.cumulative_impact,
                    "per_week": t.per_week,
                    "label_hint": t.label_hint,
                    "last_week_occurrences": t.last_week_occurrences,
                }
                for t in self.sustained_themes
            ],
            "reversal_events": [
                {
                    "name": e.name,
                    "kind": e.kind,
                    "flipped_from": e.flipped_from,
                    "flipped_to": e.flipped_to,
                    "flip_dates": e.flip_dates,
                    "flips_in_month": e.flips_in_month,
                }
                for e in self.reversal_events
            ],
            "industry_cumulative_impact": [
                {"industry": name, "cumulative": value}
                for name, value in sorted(
                    self.industry_cumulative_impact.items(),
                    key=lambda kv: abs(kv[1]),
                    reverse=True,
                )
            ],
            "etf_monthly_summary": {
                "first_day": self.etf_monthly_summary.first_day,
                "last_day": self.etf_monthly_summary.last_day,
                "first_day_pct": self.etf_monthly_summary.first_day_pct,
                "last_day_pct": self.etf_monthly_summary.last_day_pct,
                "month_cumulative_pct": self.etf_monthly_summary.month_cumulative_pct,
                "high_day": self.etf_monthly_summary.high_day,
                "high_pct": self.etf_monthly_summary.high_pct,
                "low_day": self.etf_monthly_summary.low_day,
                "low_pct": self.etf_monthly_summary.low_pct,
            },
            "top_signals": list(self.top_signals),
            "forecast_bullets": list(self.forecast_bullets),
            "notes": list(self.notes),
        }


# ---------------------------------------------------------------------------
# Date helpers
# ---------------------------------------------------------------------------


def month_bounds(anchor: date) -> tuple[date, date, str]:
    """Return ``(first_day, last_day, label)`` for the calendar month of ``anchor``.

    ``label`` is ``'YYYY-MM'`` — used both in the digest filename and
    inside the rendered title. We use calendar months (not ISO weeks)
    because the spec asks for "first business day of next month" which
    snaps to calendar months naturally.
    """
    first = anchor.replace(day=1)
    last_day = calendar.monthrange(anchor.year, anchor.month)[1]
    last = anchor.replace(day=last_day)
    label = f"{anchor.year}-{anchor.month:02d}"
    return first, last, label


def previous_month(anchor: date) -> date:
    """Return any date inside the previous calendar month of ``anchor``.

    Convenient default for the "1st-of-month -> last-month" CLI flow.
    """
    first_of_this = anchor.replace(day=1)
    return first_of_this - timedelta(days=1)


def is_business_day(anchor: date) -> bool:
    """Cheap Mon-Fri check (excludes weekends only — no public holidays).

    Used by the wrapper script to "defer to Monday" when the 1st of
    a month is a weekend. We deliberately don't pull in a CN-holidays
    table here — that would be one more upstream dependency, and
    launchd still fires the job on weekends; the deferral logic lives
    in the shell wrapper.
    """
    return anchor.weekday() < 5  # Mon=0..Fri=4


def collect_brief_paths_for_month(
    briefs_dir: Path,
    anchor: date,
) -> list[Path]:
    """Return every ``YYYY-MM-DD.md`` brief inside ``briefs_dir`` for the month containing ``anchor``.

    Missing days are silently omitted — :func:`compose_monthly_digest`
    handles graceful degradation downstream. Only the canonical CN
    files are returned (no ``.en`` siblings).
    """
    if not briefs_dir.exists():
        return []
    first, last, _ = month_bounds(anchor)
    paths: list[Path] = []
    for p in sorted(briefs_dir.glob("*.md")):
        stem = p.stem
        if stem in {"index", "latest"}:
            continue
        if "." in stem:
            continue
        try:
            day = datetime.strptime(stem, "%Y-%m-%d").date()
        except ValueError:
            continue
        if first <= day <= last:
            paths.append(p)
    return paths


def collect_digest_paths_for_month(
    digests_dir: Path,
    anchor: date,
) -> list[Path]:
    """Return weekly digest paths whose ISO week **overlaps** the month of ``anchor``.

    A week can straddle a month boundary — e.g. W18 of 2026 starts
    2026-04-27 (April) but ends 2026-05-01 (May). We include any week
    whose Mon..Fri intersects the month range so the monthly digest
    surfaces all weeks that contributed dailies to it.
    """
    if not digests_dir.exists():
        return []
    first, last, _ = month_bounds(anchor)
    out: list[Path] = []
    for p in sorted(digests_dir.glob("*.md")):
        stem = p.stem
        if "." in stem:  # skip language siblings
            continue
        m = _WEEKLY_DIGEST_STEM_RE.fullmatch(stem)
        if not m:
            continue
        iso_year = int(m.group("year"))
        iso_week = int(m.group("week"))
        monday = _iso_week_monday(iso_year, iso_week)
        friday = monday + timedelta(days=4)
        # Overlap if [monday, friday] intersects [first, last].
        if friday < first or monday > last:
            continue
        out.append(p)
    return out


def _iso_week_monday(iso_year: int, iso_week: int) -> date:
    """Return the Monday of the given ISO year/week."""
    # Anchored on Jan 4 — that day is always in ISO week 1.
    jan4 = date(iso_year, 1, 4)
    week1_monday = jan4 - timedelta(days=jan4.isoweekday() - 1)
    return week1_monday + timedelta(weeks=iso_week - 1)


# ---------------------------------------------------------------------------
# Weekly digest parsing (lightweight — for the footer table only)
# ---------------------------------------------------------------------------


def parse_weekly_digest(path: Path) -> WeeklyDigestSummary | None:
    """Read just enough metadata from a rendered weekly digest to link to it.

    Returns ``None`` if the filename doesn't match ``YYYY-Www`` (so
    accidental files inside ``digests/`` don't crash the aggregator).
    """
    stem = path.stem
    m = _WEEKLY_DIGEST_STEM_RE.fullmatch(stem)
    if not m:
        return None
    iso_year = int(m.group("year"))
    iso_week = int(m.group("week"))

    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return WeeklyDigestSummary(
            path=path,
            iso_year=iso_year,
            iso_week=iso_week,
            week_start=None,
            week_end=None,
            etf_weekly_pct=None,
            themes_count=0,
            inflections_count=0,
        )

    week_start: date | None = None
    week_end: date | None = None
    header = _WEEKLY_HEADER_RE.search(text)
    if header:
        try:
            week_start = datetime.strptime(header.group("start"), "%Y-%m-%d").date()
            week_end = datetime.strptime(header.group("end"), "%Y-%m-%d").date()
        except ValueError:
            pass
    # Fall back to ISO calculation if the header was missing.
    if week_start is None:
        week_start = _iso_week_monday(iso_year, iso_week)
    if week_end is None:
        week_end = week_start + timedelta(days=4)

    etf_pct: float | None = None
    etf_match = _WEEKLY_ETF_RE.search(text)
    if etf_match:
        try:
            etf_pct = float(etf_match.group("pct"))
        except ValueError:
            etf_pct = None

    themes = 0
    theme_match = _WEEKLY_THEME_COUNT_RE.search(text)
    if theme_match:
        try:
            themes = int(theme_match.group("n"))
        except ValueError:
            themes = 0

    inflections = 0
    infl_match = _WEEKLY_INFLECTION_COUNT_RE.search(text)
    if infl_match:
        try:
            inflections = int(infl_match.group("n"))
        except ValueError:
            inflections = 0

    return WeeklyDigestSummary(
        path=path,
        iso_year=iso_year,
        iso_week=iso_week,
        week_start=week_start,
        week_end=week_end,
        etf_weekly_pct=etf_pct,
        themes_count=themes,
        inflections_count=inflections,
    )


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


def compose_monthly_digest(
    brief_paths: list[Path],
    digest_paths: list[Path] | None = None,
    *,
    anchor: date | None = None,
    sustained_threshold: int = DEFAULT_SUSTAINED_THRESHOLD,
    now: datetime | None = None,
) -> MonthlyDigest:
    """Read ``brief_paths`` + ``digest_paths`` and return a :class:`MonthlyDigest`.

    ``anchor`` lets callers force a specific month. When omitted, we
    infer from the earliest brief; if that's empty too we fall back
    to today. Mirrors the contract of :func:`compose_weekly_digest`.
    """
    digest_paths = digest_paths or []

    # ---- parse briefs -------------------------------------------------
    summaries = [parse_brief(p) for p in brief_paths]
    summaries = [s for s in summaries if s.parse_note is None]
    summaries.sort(key=lambda s: s.date)

    if anchor is None:
        anchor = summaries[0].date if summaries else date.today()
    first, last, label = month_bounds(anchor)
    # Re-filter to summaries that actually fall inside the month. This
    # is defensive — callers might pass over-broad path lists (e.g. an
    # entire briefs/ directory). We never want to mis-attribute a
    # day-1 brief to last month just because it was in the list.
    summaries = [s for s in summaries if first <= s.date <= last]

    # ---- parse weekly digests ----------------------------------------
    digest_summaries: list[WeeklyDigestSummary] = []
    for p in digest_paths:
        ws = parse_weekly_digest(p)
        if ws is not None:
            digest_summaries.append(ws)
    # Sort newest first so the footer reads top-down chronological
    # backwards (most recent W on top).
    digest_summaries.sort(key=lambda d: (d.iso_year, d.iso_week), reverse=True)

    # ---- sustained themes --------------------------------------------
    policy_themes = _detect_sustained_themes(
        summaries=summaries, kind="policy", threshold=sustained_threshold
    )
    inventory_themes = _detect_sustained_themes(
        summaries=summaries, kind="inventory", threshold=sustained_threshold
    )
    sustained_themes = sorted(
        policy_themes + inventory_themes,
        key=lambda t: (-t.occurrence_days, -abs(t.cumulative_impact), t.name),
    )

    # ---- reversal events --------------------------------------------
    base_inflections = _detect_inflections(summaries)
    reversal_events = _to_reversal_events(summaries, base_inflections)
    reversal_events.sort(
        key=lambda e: (-e.flips_in_month, 0 if e.kind == "policy" else 1, e.name)
    )

    # ---- cumulative impact ------------------------------------------
    cumulative_impact = _cumulative_policy_impact_monthly(summaries)

    # ---- ETF monthly summary ----------------------------------------
    etf_summary = _etf_monthly_summary(summaries)

    # ---- top signals / forecast / notes ------------------------------
    top_signals = _top_signal_lines_monthly(sustained_themes, reversal_events)
    forecast = _carry_forward_forecast(sustained_themes)

    notes: list[str] = []
    expected_workdays = _approx_workday_count(first, last)
    if summaries and len(summaries) < expected_workdays * 0.7:
        notes.append(
            f"本月仅 {len(summaries)} 份每日简报有效（预期约 {expected_workdays}）；"
            "样本稀疏，结论应谨慎参考。"
        )
    if not summaries:
        notes.append(
            f"本月共 0 份每日简报；阈值={sustained_threshold} 天。"
            "monthly digest 仅产出 placeholder — 请先生成该月份的 daily briefs。"
        )
    if not sustained_themes and summaries:
        notes.append(
            f"未发现任何主题在本月达到 {sustained_threshold} 天阈值——可能样本不足或主题切换过快。"
        )

    fetched_at = (now or datetime.utcnow()).strftime("%Y-%m-%dT%H:%M:%SZ")

    return MonthlyDigest(
        month_start=first,
        month_end=last,
        month_label=label,
        briefs_aggregated=summaries,
        digests_aggregated=digest_summaries,
        sustained_themes=sustained_themes,
        reversal_events=reversal_events,
        industry_cumulative_impact=cumulative_impact,
        etf_monthly_summary=etf_summary,
        top_signals=top_signals,
        forecast_bullets=forecast,
        notes=notes,
        fetched_at=fetched_at,
    )


# ---------------------------------------------------------------------------
# Aggregation primitives
# ---------------------------------------------------------------------------


def _detect_sustained_themes(
    *,
    summaries: list[DailyBriefSummary],
    kind: str,
    threshold: int,
) -> list[SustainedTheme]:
    """Find names appearing on ≥``threshold`` distinct days of the month.

    The per-week occurrence histogram is computed here so the renderer
    doesn't have to recompute it. We key on ``W<iso_week>`` rather
    than calendar week so labels line up with the weekly-digest stems.
    """
    by_name_days: dict[str, set[str]] = {}
    by_name_per_week: dict[str, dict[int, int]] = {}
    by_name_cumulative: dict[str, float] = {}
    by_name_label: dict[str, str | None] = {}

    for summary in summaries:
        _, _, iso_week, _ = iso_week_bounds(summary.date)
        rows: list[tuple[str, float, str]] = []
        if kind == "policy":
            for sig in summary.policy_signals:
                rows.append((sig.industry, sig.avg_impact, sig.signal))
        elif kind == "inventory":
            for sig in summary.inventory_signals:
                rows.append((sig.metal, sig.change_pct, sig.label))
        for name, value, label in rows:
            by_name_days.setdefault(name, set()).add(summary.date_iso)
            week_bucket = by_name_per_week.setdefault(name, {})
            week_bucket[iso_week] = week_bucket.get(iso_week, 0) + 1
            by_name_cumulative[name] = by_name_cumulative.get(name, 0.0) + value
            by_name_label[name] = label

    # Identify the "last week" in the month — the ISO week of the
    # latest brief day. Used to compute ``last_week_occurrences`` so
    # the carry-forward forecast knows what's still alive.
    if summaries:
        last_day = summaries[-1].date
        _, _, last_iso_week, _ = iso_week_bounds(last_day)
    else:
        last_iso_week = None

    themes: list[SustainedTheme] = []
    for name, day_set in by_name_days.items():
        if len(day_set) < threshold:
            continue
        per_week_dict = by_name_per_week.get(name, {})
        per_week = sorted(
            ((f"W{w:02d}", count) for w, count in per_week_dict.items()),
            key=lambda kv: kv[0],
        )
        last_week_occ = (
            per_week_dict.get(last_iso_week, 0) if last_iso_week is not None else 0
        )
        themes.append(
            SustainedTheme(
                name=name,
                kind=kind,
                occurrence_days=len(day_set),
                per_week=per_week,
                cumulative_impact=by_name_cumulative.get(name, 0.0),
                label_hint=by_name_label.get(name),
                last_week_occurrences=last_week_occ,
            )
        )
    return themes


def _to_reversal_events(
    summaries: list[DailyBriefSummary],
    inflections: list[Inflection],
) -> list[ReversalEvent]:
    """Re-walk per-name signal series to count **every** flip in the month.

    The v0.9 :class:`Inflection` only carries the *last* (from, to)
    pair; for monthly aggregation we want the total number of flips
    too (a metal that wobbled all month should outrank one that
    flipped once at month-end). We compute that here.
    """
    # Rebuild the same per-key series the inflection detector used.
    by_key: dict[tuple[str, str], list[tuple[str, int]]] = {}
    for s in summaries:
        for sig in s.policy_signals:
            sign = sig.sign
            if sign == 0:
                continue
            by_key.setdefault((sig.industry, "policy"), []).append(
                (s.date_iso, sign)
            )
        for sig in s.inventory_signals:
            sign = sig.sign
            if sign == 0:
                continue
            by_key.setdefault((sig.metal, "inventory"), []).append(
                (s.date_iso, sign)
            )

    flip_counts: dict[tuple[str, str], int] = {}
    for key, series in by_key.items():
        flips = 0
        last_sign = series[0][1] if series else 0
        for _, sign in series[1:]:
            if sign != last_sign:
                flips += 1
                last_sign = sign
        flip_counts[key] = flips

    out: list[ReversalEvent] = []
    for infl in inflections:
        key = (infl.name, infl.kind)
        out.append(
            ReversalEvent(
                name=infl.name,
                kind=infl.kind,
                flipped_from=infl.flipped_from,
                flipped_to=infl.flipped_to,
                flip_dates=list(infl.flip_dates),
                flips_in_month=flip_counts.get(key, 0),
            )
        )
    return out


def _cumulative_policy_impact_monthly(
    summaries: list[DailyBriefSummary],
) -> dict[str, float]:
    """Sum per-industry ``avg_impact`` across the month."""
    out: dict[str, float] = {}
    for s in summaries:
        for sig in s.policy_signals:
            out[sig.industry] = out.get(sig.industry, 0.0) + sig.avg_impact
    return out


def _etf_monthly_summary(summaries: list[DailyBriefSummary]) -> ETFMonthlySummary:
    """Compute ETF NAV month-over-month summary.

    ``month_cumulative_pct`` is the sum of daily NAV % moves — same
    formula as the weekly digest's cumulative netflow. We DON'T
    multiply (1+r) because the underlying daily numbers are already
    decimal percents and the brief never publishes the absolute NAV
    on a stable scale. For an end-user-facing "month change" we
    surface ``last_day_pct - first_day_pct`` as a separate field.
    """
    daily = [
        (s.date_iso, s.etf_daily_return_pct)
        for s in summaries
        if s.etf_daily_return_pct is not None
    ]
    if not daily:
        return ETFMonthlySummary(
            first_day=None,
            last_day=None,
            first_day_pct=None,
            last_day_pct=None,
            month_cumulative_pct=None,
            high_day=None,
            high_pct=None,
            low_day=None,
            low_pct=None,
        )

    first_day, first_pct = daily[0]
    last_day, last_pct = daily[-1]
    high_day, high_pct = max(daily, key=lambda kv: kv[1])
    low_day, low_pct = min(daily, key=lambda kv: kv[1])
    month_cum = sum(v for _, v in daily)

    return ETFMonthlySummary(
        first_day=first_day,
        last_day=last_day,
        first_day_pct=first_pct,
        last_day_pct=last_pct,
        month_cumulative_pct=month_cum,
        high_day=high_day,
        high_pct=high_pct,
        low_day=low_day,
        low_pct=low_pct,
    )


def _top_signal_lines_monthly(
    sustained_themes: list[SustainedTheme],
    reversal_events: list[ReversalEvent],
) -> list[str]:
    """Headline bullets surfaced at the top of the monthly digest body."""
    lines: list[str] = []
    if sustained_themes:
        top = sustained_themes[0]
        kind_cn = "政策" if top.kind == "policy" else "库存"
        lines.append(
            f"本月{kind_cn}核心主题：**{top.name}**（出现 {top.occurrence_days} "
            f"天 · 累计 {top.cumulative_impact:+.3f}）。"
        )
    if reversal_events:
        evt = reversal_events[0]
        kind_cn = "政策" if evt.kind == "policy" else "库存"
        if evt.flips_in_month >= 2:
            lines.append(
                f"反转最频繁：**{evt.name}**（{kind_cn}）在本月内方向翻转 "
                f"{evt.flips_in_month} 次，说明信号尚未稳定。"
            )
        else:
            direction = "由正转负" if evt.flipped_to < 0 else "由负转正"
            lines.append(
                f"信号反转：**{evt.name}**（{kind_cn}）在本月 {direction}，"
                f"翻转日期={', '.join(evt.flip_dates)}。"
            )
    if not lines:
        lines.append("本月未发现持续 12 天以上的主题，亦无信号反转——属于另类数据的安静一月。")
    return lines


def _carry_forward_forecast(
    sustained_themes: list[SustainedTheme],
) -> list[str]:
    """Deterministic "下月观察" bullets — themes still alive at month-end."""
    out: list[str] = []
    for theme in sustained_themes:
        if theme.last_week_occurrences >= CARRY_FORWARD_LAST_WEEK_THRESHOLD:
            kind_cn = "政策" if theme.kind == "policy" else "库存"
            out.append(
                f"**{theme.name}**（{kind_cn}口径） — 本月持续 {theme.occurrence_days} 天，"
                f"且月末最后一周仍出现 {theme.last_week_occurrences} 次，下月值得继续跟踪。"
            )
    if not out:
        out.append("月末无延续到下月的强主题；按既定日报/周报节奏即可。")
    return out


def _approx_workday_count(first: date, last: date) -> int:
    """Cheap Mon..Fri count between ``first`` and ``last`` (inclusive).

    Used only for the "did we miss a lot of days?" note in the
    digest's body. We don't subtract public holidays — better an
    over-cautious note than a missing one.
    """
    count = 0
    day = first
    while day <= last:
        if day.weekday() < 5:
            count += 1
        day += timedelta(days=1)
    return count
