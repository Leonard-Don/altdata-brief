"""v0.11 — monthly digest tests.

Covers the deterministic synthesis path end-to-end:

* parsing daily briefs across an entire calendar month,
* 12-day sustained theme detection,
* within-month reversal events (count every flip, not just last),
* cumulative impact aggregation across all dailies,
* ETF NAV month-over-month decomposition (first / last / high / low),
* carry-forward "下月观察" forecast (sustained themes alive in last week),
* graceful degradation on empty inputs,
* CLI surface (``cn-altdata-brief monthly-digest``),
* launchd installer renders the 1st-of-month plist with Day=1.

Tests synthesize their own CN brief markdown files so the digest
layer stays decoupled from the daily generation pipeline (same
philosophy as the v0.9 weekly digest tests).
"""

from __future__ import annotations

import calendar
import os
import subprocess
import xml.etree.ElementTree as ET
from datetime import date
from pathlib import Path

import pytest

from cn_altdata_brief.cli import main as cli_main
from cn_altdata_brief.digest import (
    CARRY_FORWARD_LAST_WEEK_THRESHOLD,
    DEFAULT_SUSTAINED_THRESHOLD,
    collect_brief_paths_for_month,
    collect_digest_paths_for_month,
    compose_monthly_digest,
    compose_weekly_digest,
    month_bounds,
    parse_weekly_digest,
    previous_month,
)
from cn_altdata_brief.render import (
    render_monthly_digest_markdown,
    render_weekly_digest_markdown,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# Brief-factory helpers (mirror the test_weekly_digest fixture shape)
# ---------------------------------------------------------------------------


def _brief(
    date_iso: str,
    *,
    policy: list[tuple[str, float, int, str]] | None = None,
    inventory: list[tuple[str, float, str, str]] | None = None,
    etf_daily_pct: float | None = None,
) -> str:
    """Synthesize a CN brief that the digest parser can read."""
    policy = policy or []
    inventory = inventory or []

    pol_lines = []
    for industry, impact, mentions, signal in policy:
        direction = "正向" if impact > 0 else ("负向" if impact < 0 else "中性")
        pol_lines.append(
            f"- **{industry}**：avg_impact={impact:+.3f} ({direction}) · "
            f"mentions={mentions} · 信号={signal}"
        )
    pol_block = "\n".join(pol_lines) if pol_lines else "- _数据缺失_：placeholder"

    inv_lines = []
    for metal, change, trend, label in inventory:
        inv_lines.append(
            f"- **{metal}**：周价格变化 {change:+.2f}% · 波动率 0.0 · "
            f"趋势={trend} · 标签={label} · conf=0.10"
        )
    inv_block = "\n".join(inv_lines) if inv_lines else "- _数据缺失_：placeholder"

    if etf_daily_pct is not None:
        nav_line = (
            f"- NAV ({date_iso}) · 单位净值 2.200 · "
            f"日收益 {etf_daily_pct:+.2f}%"
        )
    else:
        nav_line = "- NAV (n/a) · 单位净值 n/a · 日收益 n/a"

    return (
        f"# CN AltData Brief — {date_iso}\n\n"
        "## 1. 政策动向\n\n"
        f"{pol_block}\n\n"
        "**Sources:** super-pricing-system\n\n"
        "---\n"
        "## 2. 库存信号\n\n"
        f"{inv_block}\n\n"
        "**Sources:** super-pricing-system\n\n"
        "---\n"
        "## 3. ETF 资金流\n\n"
        "- **有色金属ETF南方** (512400) · 现价 2.200\n"
        f"{nav_line}\n\n"
        "**Sources:** ETF-512400\n\n"
        "---\n"
        "## 4. 行业温度\n\n"
        "- _数据缺失_\n\n"
        "---\n"
        "## 5. 本日观察\n\n"
        "> 本日观察文本\n\n"
        "---\n"
    )


def _workdays_in_month(year: int, month: int) -> list[date]:
    """Return Mon..Fri dates inside the given calendar month."""
    last_day = calendar.monthrange(year, month)[1]
    out = []
    for d in range(1, last_day + 1):
        day = date(year, month, d)
        if day.weekday() < 5:
            out.append(day)
    return out


@pytest.fixture
def month_dir(tmp_path: Path) -> tuple[Path, Path]:
    """Write synthetic dailies + weeklies for the month of 2026-04.

    April 2026 has 22 workdays (Wed Apr 1 → Thu Apr 30). Designed
    properties:

    * **新能源汽车** appears in 政策 on every workday (22/22) — well
      above the 12-day sustained threshold; also runs into the last
      week so it triggers carry-forward.
    * **铝** appears in 库存 on every workday (22/22) — sustained.
    * **电网** appears 15/22 days but flips sign multiple times —
      sustained + heavy reversal event.
    * **AI算力** appears on 10/22 days — does NOT qualify as a
      monthly theme.
    * **铜** appears 5 days (only first week) — does NOT qualify; no
      flips after week 1.
    * **不锈钢** appears once — filtered out.
    * ETF NAV records daily moves with a clear month-end uptick.
    """
    briefs = tmp_path / "briefs"
    briefs.mkdir()
    digests = tmp_path / "digests"
    digests.mkdir()

    workdays = _workdays_in_month(2026, 4)
    # Sanity: April 2026 has 22 workdays.
    assert len(workdays) == 22

    # Reversal pattern for 电网: flip 3 times across the month.
    # Pattern: [+, +, +, -, -, +, +, -, -, +, +, +, +, +, +]
    dianwang_signs = [+1, +1, +1, -1, -1, +1, +1, -1, -1, +1, +1, +1, +1, +1, +1]

    for idx, day in enumerate(workdays):
        date_iso = day.isoformat()

        policy_rows: list[tuple[str, float, int, str]] = [
            ("新能源汽车", -0.30 - 0.005 * idx, 80 + idx, "利空"),
        ]

        # 电网 appears 15/22 days with a flipping sign series.
        if idx < 15:
            sign = dianwang_signs[idx]
            policy_rows.append(("电网", 0.15 * sign, 10 + idx, "中性"))

        # AI算力 appears 10/22 days — below the 12-day threshold.
        if idx < 10:
            policy_rows.append(("AI算力", +0.05 + 0.01 * idx, 5 + idx, "中性"))

        # 不锈钢 appears once — filtered.
        if idx == 0:
            policy_rows.append(("不锈钢", -0.02, 1, "中性"))

        inventory_rows: list[tuple[str, float, str, str]] = [
            ("铝", -1.0 - 0.02 * idx, "stable", "去库"),
        ]
        # 铜 appears only in week 1 (5 trading days) — too short for sustained.
        if idx < 5:
            inventory_rows.append(
                ("铜", -0.85 + 0.10 * idx, "stable", "持稳")
            )

        # ETF NAV: small daily moves; clear month-end rally.
        etf_pct = 0.20 + 0.01 * idx
        if idx == 7:
            etf_pct = 1.85  # high day
        if idx == 16:
            etf_pct = -1.10  # low day

        (briefs / f"{date_iso}.md").write_text(
            _brief(
                date_iso,
                policy=policy_rows,
                inventory=inventory_rows,
                etf_daily_pct=etf_pct,
            ),
            encoding="utf-8",
        )

    # Render 4 synthetic weekly digests that overlap the month so the
    # monthly aggregator can summarize them in the footer.
    for anchor in (date(2026, 4, 3), date(2026, 4, 10), date(2026, 4, 17), date(2026, 4, 24)):
        paths_in_week = sorted(briefs.glob("*.md"))
        weekly = compose_weekly_digest(paths_in_week, anchor=anchor)
        md = render_weekly_digest_markdown(context=weekly.render_context())
        stem = f"{weekly.iso_year}-W{weekly.week_number:02d}"
        (digests / f"{stem}.md").write_text(md, encoding="utf-8")

    return briefs, digests


# ---------------------------------------------------------------------------
# Date helpers
# ---------------------------------------------------------------------------


def test_month_bounds_april_2026() -> None:
    first, last, label = month_bounds(date(2026, 4, 15))
    assert first == date(2026, 4, 1)
    assert last == date(2026, 4, 30)
    assert label == "2026-04"


def test_month_bounds_december_2026_handles_year_boundary() -> None:
    first, last, label = month_bounds(date(2026, 12, 20))
    assert first == date(2026, 12, 1)
    assert last == date(2026, 12, 31)
    assert label == "2026-12"


def test_previous_month_handles_january() -> None:
    # Jan 5 → some day in Dec of previous year.
    prev = previous_month(date(2026, 1, 5))
    assert prev.year == 2025
    assert prev.month == 12


# ---------------------------------------------------------------------------
# Path collection
# ---------------------------------------------------------------------------


def test_collect_brief_paths_for_month_filters_correctly(month_dir: tuple[Path, Path]) -> None:
    briefs, _ = month_dir
    # Add a stray brief outside the month — must be ignored.
    (briefs / "2026-05-04.md").write_text(
        _brief("2026-05-04", policy=[("新能源汽车", -0.4, 90, "利空")]),
        encoding="utf-8",
    )
    paths = collect_brief_paths_for_month(briefs, date(2026, 4, 15))
    # 22 workdays in Apr 2026; the May entry is filtered.
    assert len(paths) == 22
    assert all("2026-04" in p.name for p in paths)


def test_collect_digest_paths_for_month_includes_overlapping_weeks(
    month_dir: tuple[Path, Path],
) -> None:
    _, digests = month_dir
    paths = collect_digest_paths_for_month(digests, date(2026, 4, 15))
    # Four weeks were generated above; all overlap April.
    assert len(paths) == 4


def test_parse_weekly_digest_returns_metadata(month_dir: tuple[Path, Path]) -> None:
    _, digests = month_dir
    sample = sorted(digests.glob("*.md"))[0]
    summary = parse_weekly_digest(sample)
    assert summary is not None
    assert summary.iso_year == 2026
    assert summary.week_start is not None
    assert summary.themes_count >= 0


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


def test_compose_monthly_digest_detects_12_day_sustained_themes(
    month_dir: tuple[Path, Path],
) -> None:
    briefs, digests = month_dir
    brief_paths = collect_brief_paths_for_month(briefs, date(2026, 4, 15))
    digest_paths = collect_digest_paths_for_month(digests, date(2026, 4, 15))
    monthly = compose_monthly_digest(
        brief_paths, digest_paths, anchor=date(2026, 4, 15)
    )

    names = {t.name for t in monthly.sustained_themes}
    # 新能源汽车 (22d), 铝 (22d), 电网 (15d) all clear the 12-day bar.
    assert "新能源汽车" in names
    assert "铝" in names
    assert "电网" in names
    # AI算力 = 10 days, below threshold; 铜 = 5 days; 不锈钢 = 1 day.
    assert "AI算力" not in names
    assert "铜" not in names
    assert "不锈钢" not in names

    by_name = {t.name: t.occurrence_days for t in monthly.sustained_themes}
    assert by_name["新能源汽车"] == 22
    assert by_name["铝"] == 22
    assert by_name["电网"] == 15

    # Default threshold constant is 12.
    assert DEFAULT_SUSTAINED_THRESHOLD == 12


def test_compose_monthly_digest_threshold_can_be_raised(
    month_dir: tuple[Path, Path],
) -> None:
    briefs, digests = month_dir
    brief_paths = collect_brief_paths_for_month(briefs, date(2026, 4, 15))
    digest_paths = collect_digest_paths_for_month(digests, date(2026, 4, 15))
    monthly = compose_monthly_digest(
        brief_paths,
        digest_paths,
        anchor=date(2026, 4, 15),
        sustained_threshold=20,
    )
    names = {t.name for t in monthly.sustained_themes}
    # At 20-day threshold, only the every-workday names survive.
    assert "新能源汽车" in names
    assert "铝" in names
    assert "电网" not in names  # 15 days < 20


def test_compose_monthly_digest_reversal_events_count_all_flips(
    month_dir: tuple[Path, Path],
) -> None:
    briefs, _ = month_dir
    brief_paths = collect_brief_paths_for_month(briefs, date(2026, 4, 15))
    monthly = compose_monthly_digest(brief_paths, [], anchor=date(2026, 4, 15))

    by_name = {e.name: e for e in monthly.reversal_events}
    # 电网 pattern: + + + - - + + - - + + + + + + → 4 flips.
    assert "电网" in by_name
    assert by_name["电网"].flips_in_month == 4

    # 新能源汽车 stays negative all month — must NOT be in reversals.
    assert "新能源汽车" not in by_name


def test_compose_monthly_digest_cumulative_impact_is_full_month_sum(
    month_dir: tuple[Path, Path],
) -> None:
    briefs, _ = month_dir
    brief_paths = collect_brief_paths_for_month(briefs, date(2026, 4, 15))
    monthly = compose_monthly_digest(brief_paths, [], anchor=date(2026, 4, 15))

    cumulative = monthly.industry_cumulative_impact
    # 新能源汽车 daily values: -0.30 - 0.005*idx, idx=0..21
    expected = sum(-0.30 - 0.005 * idx for idx in range(22))
    assert cumulative["新能源汽车"] == pytest.approx(expected, abs=1e-6)


def test_compose_monthly_digest_etf_summary_finds_high_low(
    month_dir: tuple[Path, Path],
) -> None:
    briefs, _ = month_dir
    brief_paths = collect_brief_paths_for_month(briefs, date(2026, 4, 15))
    monthly = compose_monthly_digest(brief_paths, [], anchor=date(2026, 4, 15))
    etf = monthly.etf_monthly_summary
    assert etf.first_day is not None
    assert etf.last_day is not None
    # Day idx=7 = 2026-04-10 → 1.85% (high)
    assert etf.high_day == "2026-04-10"
    assert etf.high_pct == pytest.approx(1.85, abs=1e-3)
    # Day idx=16 = 2026-04-23 → -1.10% (low)
    assert etf.low_day == "2026-04-23"
    assert etf.low_pct == pytest.approx(-1.10, abs=1e-3)
    assert etf.month_cumulative_pct is not None


def test_compose_monthly_digest_forecast_flags_carry_forward(
    month_dir: tuple[Path, Path],
) -> None:
    briefs, _ = month_dir
    brief_paths = collect_brief_paths_for_month(briefs, date(2026, 4, 15))
    monthly = compose_monthly_digest(brief_paths, [], anchor=date(2026, 4, 15))
    # 新能源汽车 is in every workday including last week — must carry forward.
    body = "\n".join(monthly.forecast_bullets)
    assert "新能源汽车" in body
    assert CARRY_FORWARD_LAST_WEEK_THRESHOLD == 3


# ---------------------------------------------------------------------------
# Empty / degraded paths
# ---------------------------------------------------------------------------


def test_compose_monthly_digest_empty_inputs_returns_notes() -> None:
    monthly = compose_monthly_digest([], [], anchor=date(2026, 4, 15))
    assert monthly.brief_count == 0
    assert monthly.sustained_themes == []
    assert monthly.reversal_events == []
    assert monthly.notes  # at least one note about emptiness
    # Render must succeed without StrictUndefined errors.
    md = render_monthly_digest_markdown(context=monthly.render_context())
    assert "上月回顾" in md
    assert "2026-04" in md


def test_compose_monthly_digest_skips_files_outside_month(tmp_path: Path) -> None:
    briefs = tmp_path / "briefs"
    briefs.mkdir()
    # Three briefs, only one inside the target month.
    (briefs / "2026-03-30.md").write_text(
        _brief("2026-03-30", policy=[("新能源汽车", -0.4, 90, "利空")]), encoding="utf-8"
    )
    (briefs / "2026-04-15.md").write_text(
        _brief("2026-04-15", policy=[("新能源汽车", -0.5, 95, "利空")]), encoding="utf-8"
    )
    (briefs / "2026-05-01.md").write_text(
        _brief("2026-05-01", policy=[("新能源汽车", -0.3, 80, "利空")]), encoding="utf-8"
    )
    paths = sorted(briefs.glob("*.md"))
    monthly = compose_monthly_digest(paths, [], anchor=date(2026, 4, 15))
    assert monthly.brief_count == 1
    assert monthly.briefs_aggregated[0].date == date(2026, 4, 15)


# ---------------------------------------------------------------------------
# Render template
# ---------------------------------------------------------------------------


def test_render_monthly_template_contains_expected_sections(
    month_dir: tuple[Path, Path],
) -> None:
    briefs, digests = month_dir
    brief_paths = collect_brief_paths_for_month(briefs, date(2026, 4, 15))
    digest_paths = collect_digest_paths_for_month(digests, date(2026, 4, 15))
    monthly = compose_monthly_digest(
        brief_paths, digest_paths, anchor=date(2026, 4, 15)
    )
    md = render_monthly_digest_markdown(context=monthly.render_context())
    assert "上月回顾 2026-04" in md
    assert "2026-04-01 → 2026-04-30" in md
    assert "## 本月核心信号" in md
    assert "## 1. 月度核心主题" in md
    assert "## 2. 信号反转事件" in md
    assert "## 3. 行业累计影响排行" in md
    assert "## 4. ETF 资金流月度变化" in md
    assert "## 5. 下月观察" in md
    # Every monthly digest must link back to constituent dailies + weeklies.
    assert "[2026-04-01.md](../briefs/2026-04-01.md)" in md
    assert "2026-W14" in md or "2026-W15" in md or "2026-W16" in md  # at least one weekly


# ---------------------------------------------------------------------------
# CLI surface
# ---------------------------------------------------------------------------


def test_cli_monthly_digest_writes_default_filename(
    month_dir: tuple[Path, Path],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    briefs, digests = month_dir
    out_dir = tmp_path / "out"
    rc = cli_main(
        [
            "monthly-digest",
            "--month-of",
            "2026-04",
            "--briefs-dir",
            str(briefs),
            "--digests-dir",
            str(out_dir),
        ]
    )
    assert rc == 0
    captured = capsys.readouterr().out
    assert "2026-04" in captured
    written = out_dir / "2026-04.md"
    assert written.exists()
    body = written.read_text(encoding="utf-8")
    assert "上月回顾 2026-04" in body


def test_cli_monthly_digest_accepts_ymd(
    month_dir: tuple[Path, Path],
    tmp_path: Path,
) -> None:
    briefs, _ = month_dir
    out_dir = tmp_path / "out"
    rc = cli_main(
        [
            "monthly-digest",
            "--month-of",
            "2026-04-15",
            "--briefs-dir",
            str(briefs),
            "--digests-dir",
            str(out_dir),
        ]
    )
    assert rc == 0
    assert (out_dir / "2026-04.md").exists()


def test_cli_monthly_digest_rejects_bad_month_arg(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc = cli_main(
        [
            "monthly-digest",
            "--month-of",
            "2026/04",
            "--briefs-dir",
            str(tmp_path / "briefs"),
            "--digests-dir",
            str(tmp_path / "digests"),
        ]
    )
    assert rc == 2
    err = capsys.readouterr().err
    assert "ERROR" in err


# ---------------------------------------------------------------------------
# launchd installer — monthly plist
# ---------------------------------------------------------------------------


def _emit_install_plists(target_dir: Path) -> Path:
    """Render installer plists with a fake HOME + launchctl, like weekly test."""
    env = os.environ.copy()
    env["HOME"] = str(target_dir)
    fake_bin = target_dir / "fake-bin"
    fake_bin.mkdir(parents=True, exist_ok=True)
    fake_launchctl = fake_bin / "launchctl"
    fake_launchctl.write_text(
        "#!/usr/bin/env bash\n"
        'if [ "${1:-}" = "list" ]; then exit 0; fi\n'
        "exit 0\n",
        encoding="utf-8",
    )
    fake_launchctl.chmod(0o755)
    env["PATH"] = f"{fake_bin}:{env.get('PATH', '')}"
    result = subprocess.run(
        ["/bin/bash", str(PROJECT_ROOT / "scripts" / "install_launchd_macos.sh")],
        env=env,
        capture_output=True,
        text=True,
        cwd=str(PROJECT_ROOT),
    )
    assert result.returncode in (0, 3), (
        f"installer exited unexpectedly with {result.returncode}\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}\n"
    )
    return target_dir / "Library" / "LaunchAgents"


@pytest.mark.skipif(
    os.uname().sysname != "Darwin",
    reason="launchd is a macOS-only subsystem",
)
def test_launchd_monthly_plist_renders_day_1_17_00(tmp_path: Path) -> None:
    plist_dir = _emit_install_plists(tmp_path)
    monthly = plist_dir / "com.leonardodon.cn-altdata-brief.monthly.plist"
    assert monthly.exists()
    root = ET.parse(monthly).getroot()
    body = root.find("dict")
    assert body is not None

    children = list(body)
    schedule_array = None
    for idx, child in enumerate(children):
        if child.tag == "key" and child.text == "StartCalendarInterval":
            schedule_array = children[idx + 1]
            break
    assert schedule_array is not None
    entries = schedule_array.findall("dict")
    assert len(entries) == 1, "monthly job should fire on exactly one schedule"
    entry = entries[0]
    kv = {
        k.text: v.text
        for k, v in zip(entry.findall("key"), entry.findall("integer"), strict=True)
    }
    assert kv["Day"] == "1"
    assert kv["Hour"] == "17"
    assert kv["Minute"] == "0"

    args = body.find(".//array")
    assert args is not None
    program_args = [s.text for s in args.findall("string") if s.text]
    assert any("monthly_digest_now.sh" in a for a in program_args)


@pytest.mark.skipif(
    os.uname().sysname != "Darwin",
    reason="launchd is a macOS-only subsystem",
)
def test_launchd_installer_now_installs_three_plists(tmp_path: Path) -> None:
    plist_dir = _emit_install_plists(tmp_path)
    expected = {
        "com.leonardodon.cn-altdata-brief.plist",
        "com.leonardodon.cn-altdata-brief.weekly.plist",
        "com.leonardodon.cn-altdata-brief.monthly.plist",
    }
    on_disk = {p.name for p in plist_dir.glob("com.leonardodon.cn-altdata-brief*.plist")}
    assert expected <= on_disk
