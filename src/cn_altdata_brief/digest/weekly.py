"""v0.9 — deterministic weekly digest synthesis.

The weekly digest answers a different question than the daily brief.
Where the daily brief says "what happened today across 4 alt-data
sources?", the weekly digest says "across 5 daily briefs, which
industries and metals showed *recurring* signal, and which signals
*flipped direction* mid-week?".

Parsing strategy
----------------

We read the **already-written CN brief markdown** rather than the
upstream adapter caches because:

1. The brief is the published artifact — if it omits a section, the
   digest should mirror that omission.
2. Re-fetching adapters would couple the digest to upstream availability
   on Fridays only; the daily pipeline already smoothed that out.
3. Disk-coupled iteration is cheap (5 files, ~3 kB each).

Regex extraction is brittle by design — the daily template emits a
predictable bulleted structure (``- **<name>**：<key>=<value> · ...``)
and the digest snaps to that contract. If the daily template ever
changes shape, the digest tests will fail loudly and force a re-think.

Aggregation rules (v0.9, deterministic)
---------------------------------------

* **Theme** = an industry / metal / instrument that appears in the
  ``政策动向`` or ``库存信号`` bullets on ≥3 distinct trading days of the
  week. The theme record keeps the per-day impact / change values so
  the renderer can plot a mini sparkline-in-prose.
* **Inflection** = a (industry, signal_kind) pair whose sign flipped at
  least once between consecutive daily briefs. Sign is computed from
  the polarity of the underlying numeric (avg_impact for policy;
  price_change_pct for inventory). A no-data day in the middle does
  NOT count as a flip — we ignore missing values when measuring
  consecutive directionality.
* **Cumulative impact** = sum of per-day avg_impact contributions for
  each industry that appeared at least once. This is *not* a forecast;
  it is a cumulative footprint useful for "who was loudest this week".
* **ETF netflow summary** = sum of daily NAV percentage moves across
  the week. We add a synthetic forecast bullet ("X signal persisted N
  days → watch next week") when any theme spanned ≥4 days.

The output is a :class:`WeeklyDigest` dataclass with raw aggregations
plus a frozen ``render_context()`` payload, which the Jinja template
consumes via :func:`render_weekly_digest_markdown`.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Number of distinct trading days an industry must appear on before
# it qualifies as a "theme". v0.9 default is 3 (a majority of a
# 5-day week). Tests use this constant directly so changes are
# auditable.
DEFAULT_RECURRENCE_THRESHOLD = 3

# Number of consecutive days a theme must persist before the
# "下周展望" section flags it for follow-up next week.
FORECAST_PERSISTENCE_THRESHOLD = 4

# Regexes that parse the deterministic brief format. Each regex is
# anchored to a section heading so we can scope extraction to the
# correct block — the daily brief uses ``## 1. 政策动向`` /
# ``## 2. 库存信号`` / ``## 3. ETF 资金流`` style headings.

_POLICY_SECTION_RE = re.compile(
    r"## 1\. 政策动向(.*?)(?:^## 2\.)",
    flags=re.DOTALL | re.MULTILINE,
)
_INVENTORY_SECTION_RE = re.compile(
    r"## 2\. 库存信号(.*?)(?:^## 3\.)",
    flags=re.DOTALL | re.MULTILINE,
)
_ETF_SECTION_RE = re.compile(
    r"## 3\. ETF 资金流(.*?)(?:^## 4\.)",
    flags=re.DOTALL | re.MULTILINE,
)

# Per-bullet patterns inside each section. The daily template emits:
#   - **<industry>**：avg_impact=±0.388 (负向) · mentions=94 · 信号=利空
#   - **<metal>**：周价格变化 +1.20% · 波动率 ...
# The ETF section's NAV line is unique:
#   - NAV (2026-05-06) · 单位净值 2.1985 · 日收益 +3.85%

_POLICY_BULLET_RE = re.compile(
    r"-\s+\*\*(?P<name>[^*]+)\*\*[^\n]*?avg_impact=(?P<impact>[+-]?\d+(?:\.\d+)?)"
    r"[^\n]*?mentions=(?P<mentions>\d+)[^\n]*?信号=(?P<signal>[^\s·]+)"
)

_INVENTORY_BULLET_RE = re.compile(
    r"-\s+\*\*(?P<name>[^*]+)\*\*[^\n]*?周价格变化\s+(?P<change>[+-]?\d+(?:\.\d+)?)%"
    r"[^\n]*?趋势=(?P<trend>\S+)\s+·\s+标签=(?P<label>\S+)"
)

_NAV_LINE_RE = re.compile(
    r"NAV\s+\(([^)]+)\)[^\n]*?日收益\s+(?P<daily>[+-]?\d+(?:\.\d+)?)%",
)


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class PolicySignal:
    """One per-industry policy row parsed from a daily brief."""

    industry: str
    avg_impact: float
    mentions: int
    signal: str  # 利好 / 利空 / 中性 — Chinese label from the brief

    @property
    def sign(self) -> int:
        """-1 / 0 / +1 sign of ``avg_impact`` (used by inflection detection)."""
        if abs(self.avg_impact) < 1e-9:
            return 0
        return 1 if self.avg_impact > 0 else -1


@dataclass(slots=True)
class InventorySignal:
    """One per-metal inventory row parsed from a daily brief."""

    metal: str
    change_pct: float
    trend: str
    label: str  # 累库 / 去库 / 持稳

    @property
    def sign(self) -> int:
        if abs(self.change_pct) < 1e-9:
            return 0
        return 1 if self.change_pct > 0 else -1


@dataclass(slots=True)
class DailyBriefSummary:
    """Structured view of one daily brief.

    Carries everything the digest needs without re-reading the file.
    Missing sections show up as empty lists / ``None`` so the
    aggregation logic stays branch-free.
    """

    path: Path
    date: date
    policy_signals: list[PolicySignal] = field(default_factory=list)
    inventory_signals: list[InventorySignal] = field(default_factory=list)
    etf_daily_return_pct: float | None = None
    parse_note: str | None = None

    @property
    def date_iso(self) -> str:
        return self.date.isoformat()

    @property
    def is_empty(self) -> bool:
        """True when the file had no recognizable signal bullets at all."""
        return (
            not self.policy_signals
            and not self.inventory_signals
            and self.etf_daily_return_pct is None
        )


@dataclass(slots=True)
class Theme:
    """An industry / metal that appeared in ≥N daily briefs this week.

    Carries the per-day datapoints so the renderer can show a brief
    "Mon: +0.2 · Tue: +0.3 · Wed: missing" sparkline.
    """

    name: str
    kind: str  # 'policy' or 'inventory'
    occurrence_days: int
    per_day: list[tuple[str, float | None]]  # [(date_iso, value | None), ...]
    cumulative_impact: float
    label_hint: str | None = None  # e.g. last seen label (利空 / 累库)


@dataclass(slots=True)
class Inflection:
    """A (name, kind) whose signal sign flipped mid-week."""

    name: str
    kind: str  # 'policy' or 'inventory'
    flipped_from: int  # sign before flip (-1 / +1)
    flipped_to: int
    flip_dates: list[str]  # [date_iso_before, date_iso_after, ...]


@dataclass(slots=True)
class WeeklyDigest:
    """The full deterministic weekly digest payload."""

    week_start: date
    week_end: date
    week_number: int
    iso_year: int
    briefs_aggregated: list[DailyBriefSummary] = field(default_factory=list)
    themes: list[Theme] = field(default_factory=list)
    inflections: list[Inflection] = field(default_factory=list)
    cumulative_policy_impact: dict[str, float] = field(default_factory=dict)
    etf_weekly_cumulative_pct: float | None = None
    top_signals: list[str] = field(default_factory=list)
    forecast_bullets: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    fetched_at: str = ""

    @property
    def week_start_iso(self) -> str:
        return self.week_start.isoformat()

    @property
    def week_end_iso(self) -> str:
        return self.week_end.isoformat()

    @property
    def brief_count(self) -> int:
        """Count of briefs that contributed at least one signal."""
        return sum(1 for b in self.briefs_aggregated if not b.is_empty)

    def render_context(self) -> dict[str, Any]:
        """Frozen dict passed straight into the Jinja template."""
        return {
            "week_number": self.week_number,
            "iso_year": self.iso_year,
            "week_start": self.week_start_iso,
            "week_end": self.week_end_iso,
            "fetched_at": self.fetched_at,
            "brief_count": self.brief_count,
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
            "themes": [
                {
                    "name": t.name,
                    "kind": t.kind,
                    "occurrence_days": t.occurrence_days,
                    "cumulative_impact": t.cumulative_impact,
                    "per_day": t.per_day,
                    "label_hint": t.label_hint,
                }
                for t in self.themes
            ],
            "inflections": [
                {
                    "name": i.name,
                    "kind": i.kind,
                    "flipped_from": i.flipped_from,
                    "flipped_to": i.flipped_to,
                    "flip_dates": i.flip_dates,
                }
                for i in self.inflections
            ],
            "cumulative_policy_impact": [
                {"industry": name, "cumulative": value}
                for name, value in sorted(
                    self.cumulative_policy_impact.items(),
                    key=lambda kv: abs(kv[1]),
                    reverse=True,
                )
            ],
            "etf_weekly_cumulative_pct": self.etf_weekly_cumulative_pct,
            "top_signals": list(self.top_signals),
            "forecast_bullets": list(self.forecast_bullets),
            "notes": list(self.notes),
        }


# ---------------------------------------------------------------------------
# Date helpers
# ---------------------------------------------------------------------------


def iso_week_bounds(anchor: date) -> tuple[date, date, int, int]:
    """Return ``(monday, friday, iso_week_number, iso_year)`` for the week containing ``anchor``.

    We anchor to Monday → Friday (workweek) rather than the ISO Sunday-
    end convention because daily briefs only run on workdays. ``iso_year``
    follows the ISO 8601 calendar — for the edge case where a Monday
    falls in week 52 of the previous ISO year, the digest filename uses
    the ISO year/week pair so the lexical sort stays right.
    """
    weekday = anchor.weekday()  # Monday=0, Sunday=6
    monday = anchor - timedelta(days=weekday)
    friday = monday + timedelta(days=4)
    iso_year, iso_week, _ = monday.isocalendar()
    return monday, friday, iso_week, iso_year


def collect_brief_paths_for_week(
    briefs_dir: Path,
    anchor: date,
) -> list[Path]:
    """Return the Mon..Fri ``YYYY-MM-DD.md`` paths existing inside ``briefs_dir``.

    Missing days are simply omitted — :func:`compose_weekly_digest`
    handles graceful degradation downstream.
    """
    monday, _, _, _ = iso_week_bounds(anchor)
    paths: list[Path] = []
    for offset in range(5):  # Mon..Fri
        day = monday + timedelta(days=offset)
        candidate = briefs_dir / f"{day.isoformat()}.md"
        if candidate.exists():
            paths.append(candidate)
    return paths


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def parse_brief(path: Path) -> DailyBriefSummary:
    """Extract structured signals from a single daily brief markdown file.

    Tolerates a degraded brief — missing sections become empty
    lists; an entirely unparseable file gets ``parse_note`` populated.
    """
    iso_str = path.stem
    # The CN brief filename is always ``YYYY-MM-DD.md``; if a non-date
    # stem ever slips in, fall back to a sentinel and let the caller see
    # the parse_note.
    try:
        day = datetime.strptime(iso_str, "%Y-%m-%d").date()
    except ValueError:
        return DailyBriefSummary(
            path=path,
            date=date(1970, 1, 1),
            parse_note=f"filename {path.name} is not YYYY-MM-DD.md",
        )

    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return DailyBriefSummary(
            path=path,
            date=day,
            parse_note=f"unreadable file ({exc})",
        )

    policy_signals = list(_iter_policy_signals(text))
    inventory_signals = list(_iter_inventory_signals(text))
    etf_pct = _extract_etf_daily_return_pct(text)

    return DailyBriefSummary(
        path=path,
        date=day,
        policy_signals=policy_signals,
        inventory_signals=inventory_signals,
        etf_daily_return_pct=etf_pct,
    )


def _iter_policy_signals(brief_md: str):
    block = _POLICY_SECTION_RE.search(brief_md)
    if not block:
        return
    for match in _POLICY_BULLET_RE.finditer(block.group(1)):
        name = match.group("name").strip()
        if name.startswith("_数据缺失"):
            continue
        try:
            impact = float(match.group("impact"))
            mentions = int(match.group("mentions"))
        except ValueError:
            continue
        yield PolicySignal(
            industry=name,
            avg_impact=impact,
            mentions=mentions,
            signal=match.group("signal"),
        )


def _iter_inventory_signals(brief_md: str):
    block = _INVENTORY_SECTION_RE.search(brief_md)
    if not block:
        return
    for match in _INVENTORY_BULLET_RE.finditer(block.group(1)):
        name = match.group("name").strip()
        if name.startswith("_数据缺失"):
            continue
        try:
            change = float(match.group("change"))
        except ValueError:
            continue
        yield InventorySignal(
            metal=name,
            change_pct=change,
            trend=match.group("trend"),
            label=match.group("label"),
        )


def _extract_etf_daily_return_pct(brief_md: str) -> float | None:
    block = _ETF_SECTION_RE.search(brief_md)
    if not block:
        return None
    nav = _NAV_LINE_RE.search(block.group(1))
    if not nav:
        return None
    try:
        return float(nav.group("daily"))
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


def compose_weekly_digest(
    brief_paths: list[Path],
    *,
    anchor: date | None = None,
    recurrence_threshold: int = DEFAULT_RECURRENCE_THRESHOLD,
    now: datetime | None = None,
) -> WeeklyDigest:
    """Read ``brief_paths`` and return a :class:`WeeklyDigest`.

    ``anchor`` lets callers force a specific week (otherwise we infer it
    from the earliest brief, or fall back to today). Missing brief paths
    are silently skipped — the digest's ``brief_count`` will reveal
    short weeks to the renderer.
    """
    summaries = [parse_brief(p) for p in brief_paths]
    summaries = [s for s in summaries if s.parse_note is None]
    summaries.sort(key=lambda s: s.date)

    if anchor is None:
        anchor = summaries[0].date if summaries else date.today()
    monday, friday, week_num, iso_year = iso_week_bounds(anchor)

    # ---- themes -------------------------------------------------------
    policy_themes = _detect_themes(
        summaries=summaries,
        kind="policy",
        recurrence_threshold=recurrence_threshold,
    )
    inventory_themes = _detect_themes(
        summaries=summaries,
        kind="inventory",
        recurrence_threshold=recurrence_threshold,
    )
    themes = sorted(
        policy_themes + inventory_themes,
        key=lambda t: (-t.occurrence_days, -abs(t.cumulative_impact), t.name),
    )

    # ---- inflections --------------------------------------------------
    inflections = _detect_inflections(summaries)

    # ---- cumulative impact + ETF netflow -----------------------------
    cumulative = _cumulative_policy_impact(summaries)
    etf_cum = _cumulative_etf_netflow(summaries)

    # ---- top signals + forecast --------------------------------------
    top_signals = _top_signal_lines(themes, inflections)
    forecast = _forecast_lines(themes, inflections)

    # ---- notes (short-week annotation, missing-day warnings) ---------
    notes: list[str] = []
    days_present = sum(1 for s in summaries if not s.is_empty)
    if days_present < 5:
        notes.append(
            f"本周仅 {days_present}/5 份每日简报提供有效信号，主题筛选阈值"
            f"=连续 {recurrence_threshold} 日。"
        )
    if not themes:
        notes.append(
            "未发现任何行业 / 金属在本周达到 "
            f"{recurrence_threshold} 日重复阈值；可能上游样本稀疏或一周内主题切换过快。"
        )

    fetched_at = (now or datetime.utcnow()).strftime("%Y-%m-%dT%H:%M:%SZ")

    return WeeklyDigest(
        week_start=monday,
        week_end=friday,
        week_number=week_num,
        iso_year=iso_year,
        briefs_aggregated=summaries,
        themes=themes,
        inflections=inflections,
        cumulative_policy_impact=cumulative,
        etf_weekly_cumulative_pct=etf_cum,
        top_signals=top_signals,
        forecast_bullets=forecast,
        notes=notes,
        fetched_at=fetched_at,
    )


# ---------------------------------------------------------------------------
# Aggregation primitives
# ---------------------------------------------------------------------------


def _detect_themes(
    *,
    summaries: list[DailyBriefSummary],
    kind: str,
    recurrence_threshold: int,
) -> list[Theme]:
    """Find names appearing on ≥``recurrence_threshold`` distinct days.

    Each ``Theme`` carries the per-day raw value (avg_impact for policy,
    change_pct for inventory) so the renderer can plot the journey.
    """
    by_name: dict[str, list[tuple[str, float, str]]] = {}
    for summary in summaries:
        rows: list[tuple[str, float, str]] = []
        if kind == "policy":
            for sig in summary.policy_signals:
                rows.append((sig.industry, sig.avg_impact, sig.signal))
        elif kind == "inventory":
            for sig in summary.inventory_signals:
                rows.append((sig.metal, sig.change_pct, sig.label))
        for name, value, label in rows:
            by_name.setdefault(name, []).append((summary.date_iso, value, label))

    themes: list[Theme] = []
    for name, datapoints in by_name.items():
        unique_days = {iso for iso, _, _ in datapoints}
        if len(unique_days) < recurrence_threshold:
            continue
        all_days_in_week = sorted({s.date_iso for s in summaries})
        value_by_day = {iso: value for iso, value, _ in datapoints}
        per_day: list[tuple[str, float | None]] = [
            (day, value_by_day.get(day)) for day in all_days_in_week
        ]
        cumulative = sum(value for _, value, _ in datapoints)
        label_hint = datapoints[-1][2] if datapoints else None
        themes.append(
            Theme(
                name=name,
                kind=kind,
                occurrence_days=len(unique_days),
                per_day=per_day,
                cumulative_impact=cumulative,
                label_hint=label_hint,
            )
        )
    return themes


def _detect_inflections(summaries: list[DailyBriefSummary]) -> list[Inflection]:
    """Find (name, kind) pairs whose sign flipped mid-week.

    We walk consecutive non-missing observations per name. A flip is a
    sign change (-1 → +1 or +1 → -1). Zero magnitudes are treated as
    "no opinion" and skipped so a single flat day doesn't masquerade as
    an inflection.
    """
    inflections: list[Inflection] = []

    # Group by name+kind across all days, sorted chronologically.
    by_key: dict[tuple[str, str], list[tuple[str, int]]] = {}
    for s in summaries:
        for sig in s.policy_signals:
            sign = sig.sign
            if sign == 0:
                continue
            by_key.setdefault((sig.industry, "policy"), []).append((s.date_iso, sign))
        for sig in s.inventory_signals:
            sign = sig.sign
            if sign == 0:
                continue
            by_key.setdefault((sig.metal, "inventory"), []).append((s.date_iso, sign))

    for (name, kind), series in by_key.items():
        flip_dates: list[str] = []
        last_sign = series[0][1]
        last_change_from: int | None = None
        last_change_to: int | None = None
        for iso, sign in series[1:]:
            if sign != last_sign:
                if not flip_dates:
                    flip_dates.append(series[0][0])
                flip_dates.append(iso)
                last_change_from = last_sign
                last_change_to = sign
                last_sign = sign
        if last_change_from is not None and last_change_to is not None:
            inflections.append(
                Inflection(
                    name=name,
                    kind=kind,
                    flipped_from=last_change_from,
                    flipped_to=last_change_to,
                    flip_dates=flip_dates,
                )
            )
    # Stable ordering — policy first, then inventory, then alpha by name.
    inflections.sort(key=lambda x: (0 if x.kind == "policy" else 1, x.name))
    return inflections


def _cumulative_policy_impact(
    summaries: list[DailyBriefSummary],
) -> dict[str, float]:
    """Sum per-industry avg_impact across the week.

    Used by the renderer to surface "loudest" industries even when they
    didn't meet the recurrence threshold (e.g. one massive single-day
    swing). Returned dict is sorted in the render layer; here we just
    return totals.
    """
    out: dict[str, float] = {}
    for s in summaries:
        for sig in s.policy_signals:
            out[sig.industry] = out.get(sig.industry, 0.0) + sig.avg_impact
    return out


def _cumulative_etf_netflow(summaries: list[DailyBriefSummary]) -> float | None:
    """Sum daily ETF NAV % moves across the week; ``None`` if no data."""
    values = [s.etf_daily_return_pct for s in summaries if s.etf_daily_return_pct is not None]
    if not values:
        return None
    return sum(values)


def _top_signal_lines(
    themes: list[Theme],
    inflections: list[Inflection],
) -> list[str]:
    """Two-or-three liner summary used at the top of the digest body.

    The most-frequent theme (or, failing that, the most-recent
    inflection) becomes the "headline" of the week.
    """
    lines: list[str] = []
    if themes:
        top = themes[0]
        kind_cn = "政策" if top.kind == "policy" else "库存"
        lines.append(
            f"本周{kind_cn}主题集中在 **{top.name}**（出现 {top.occurrence_days}/5 天，"
            f"累计 {top.cumulative_impact:+.3f}）。"
        )
    if inflections:
        flip = inflections[0]
        direction = "由正转负" if flip.flipped_to < 0 else "由负转正"
        kind_cn = "政策" if flip.kind == "policy" else "库存"
        lines.append(
            f"信号反转：**{flip.name}**（{kind_cn}）在本周内 {direction}，"
            f"flip dates={', '.join(flip.flip_dates)}。"
        )
    if not lines:
        lines.append("本周无重复主题，亦无信号反转——属于 alt-data 的安静一周。")
    return lines


def _forecast_lines(
    themes: list[Theme],
    inflections: list[Inflection],
) -> list[str]:
    """Build deterministic "下周展望" bullets.

    A theme spanning ≥``FORECAST_PERSISTENCE_THRESHOLD`` days is flagged
    as worth watching next week. Inflections that fired in the last
    half of the week are flagged as "possibly the start of a trend".
    """
    out: list[str] = []
    for theme in themes:
        if theme.occurrence_days >= FORECAST_PERSISTENCE_THRESHOLD:
            kind_cn = "政策" if theme.kind == "policy" else "库存"
            out.append(
                f"**{theme.name}** 信号本周持续 {theme.occurrence_days} 天（{kind_cn}口径），"
                "下周值得关注是否延续。"
            )
    for flip in inflections[:3]:
        if not flip.flip_dates:
            continue
        # If the most recent flip date is the Thursday/Friday of the
        # week, the trend is fresh — flag for next-week monitoring.
        last_flip = flip.flip_dates[-1]
        try:
            flip_day = datetime.strptime(last_flip, "%Y-%m-%d").date()
        except ValueError:
            continue
        if flip_day.weekday() >= 3:  # Thu (3), Fri (4)
            direction = "转空" if flip.flipped_to < 0 else "转多"
            kind_cn = "政策" if flip.kind == "policy" else "库存"
            out.append(
                f"**{flip.name}**（{kind_cn}）在 {last_flip} 出现{direction}迹象，"
                "下周首日复核能否确认新趋势。"
            )
    if not out:
        out.append("本周无强信号需要下周强制复盘；按既定节奏跑日更即可。")
    return out
