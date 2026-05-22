"""v0.9 — weekly digest tests.

Covers the deterministic synthesis path end-to-end:

* parsing daily briefs into per-section signals,
* recurrence-based theme detection (≥3-day rule),
* mid-week sign-flip inflection detection,
* cumulative impact aggregation,
* empty-input degradation (no briefs at all),
* template rendering (no Jinja UndefinedError on the empty path),
* launchd Friday plist installation and CLI surface.

Tests construct their own synthetic CN brief markdown files rather
than depending on the upstream adapter fixtures so the digest layer
stays decoupled from the daily generation pipeline.
"""

from __future__ import annotations

import os
import subprocess
import xml.etree.ElementTree as ET
from datetime import date
from pathlib import Path

import pytest

from altdata_brief.cli import main as cli_main
from altdata_brief.digest import (
    compose_weekly_digest,
    iso_week_bounds,
    parse_brief,
)
from altdata_brief.digest.weekly import (
    FORECAST_PERSISTENCE_THRESHOLD,
    _detect_inflections,
    _detect_themes,
)
from altdata_brief.llm.translate import TranslationResult
from altdata_brief.render import render_weekly_digest_markdown

PROJECT_ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# Brief-factory helpers
# ---------------------------------------------------------------------------


def _brief(
    date_iso: str,
    *,
    policy: list[tuple[str, float, int, str]] | None = None,
    inventory: list[tuple[str, float, str, str]] | None = None,
    etf_daily_pct: float | None = None,
) -> str:
    """Synthesize a CN brief that the digest parser can read.

    Each tuple matches the daily template emit shape so we round-trip
    through the same regexes the real pipeline uses.
    """
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
        f"# AltData Brief — {date_iso}\n\n"
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


@pytest.fixture
def week_dir(tmp_path: Path) -> Path:
    """Write 5 synthetic briefs Mon..Fri of 2026-W20 into ``tmp_path/briefs``.

    Designed so that:

    * **新能源汽车** appears in 政策 every day (5/5) — qualifies as a theme.
    * **铝** appears in 库存 every day (5/5) — qualifies as a theme.
    * **电网** appears 4/5 days with a sign flip mid-week — theme + inflection.
    * **AI算力** appears 3/5 days — qualifies as a theme.
    * **铜** appears 5/5 days but flips sign Tue → Wed — inflection.
    * **不锈钢** appears only on Mon — does NOT qualify.
    """
    briefs = tmp_path / "briefs"
    briefs.mkdir()
    days = [
        (
            "2026-05-11",  # Mon
            [
                ("新能源汽车", -0.38, 90, "利空"),
                ("电网", +0.12, 10, "中性"),
                ("AI算力", +0.05, 4, "中性"),
                ("不锈钢", -0.02, 1, "中性"),
            ],
            [
                ("铜", -0.85, "stable", "去库"),
                ("铝", -1.20, "stable", "去库"),
            ],
            0.50,
        ),
        (
            "2026-05-12",  # Tue
            [
                ("新能源汽车", -0.40, 110, "利空"),
                ("电网", +0.15, 15, "中性"),
            ],
            [
                ("铜", +0.10, "stable", "持稳"),
                ("铝", -1.10, "stable", "去库"),
            ],
            0.20,
        ),
        (
            "2026-05-13",  # Wed
            [
                ("新能源汽车", -0.35, 85, "利空"),
                ("AI算力", +0.12, 8, "中性"),
            ],
            [
                ("铜", +0.40, "stable", "持稳"),
                ("铝", -0.80, "stable", "去库"),
            ],
            0.30,
        ),
        (
            "2026-05-14",  # Thu
            [
                ("新能源汽车", -0.30, 70, "利空"),
                ("电网", -0.08, 20, "利空"),
            ],
            [
                ("铜", +0.30, "stable", "持稳"),
                ("铝", -0.60, "stable", "去库"),
            ],
            0.45,
        ),
        (
            "2026-05-15",  # Fri
            [
                ("新能源汽车", -0.42, 120, "利空"),
                ("AI算力", +0.18, 14, "中性"),
                ("电网", +0.09, 5, "中性"),
            ],
            [
                ("铜", +0.20, "stable", "持稳"),
                ("铝", -0.40, "stable", "去库"),
            ],
            0.60,
        ),
    ]
    for date_iso, pol, inv, etf in days:
        (briefs / f"{date_iso}.md").write_text(
            _brief(date_iso, policy=pol, inventory=inv, etf_daily_pct=etf),
            encoding="utf-8",
        )
    return briefs


# ---------------------------------------------------------------------------
# Date / window helpers
# ---------------------------------------------------------------------------


def test_iso_week_bounds_returns_monday_friday() -> None:
    monday, friday, week, year = iso_week_bounds(date(2026, 5, 14))
    assert monday == date(2026, 5, 11)
    assert friday == date(2026, 5, 15)
    assert week == 20
    assert year == 2026


def test_iso_week_bounds_when_anchor_is_monday_already() -> None:
    monday, friday, _, _ = iso_week_bounds(date(2026, 5, 11))
    assert monday == date(2026, 5, 11)
    assert friday == date(2026, 5, 15)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def test_parse_brief_extracts_policy_inventory_and_etf(week_dir: Path) -> None:
    summary = parse_brief(week_dir / "2026-05-11.md")
    assert summary.date == date(2026, 5, 11)
    policy_names = [p.industry for p in summary.policy_signals]
    assert "新能源汽车" in policy_names
    assert "电网" in policy_names
    inv_names = [m.metal for m in summary.inventory_signals]
    assert "铜" in inv_names
    assert "铝" in inv_names
    assert summary.etf_daily_return_pct == pytest.approx(0.50, abs=0.001)


def test_parse_brief_handles_missing_etf_gracefully(tmp_path: Path) -> None:
    path = tmp_path / "2026-05-11.md"
    path.write_text(_brief("2026-05-11", policy=[("电网", 0.1, 5, "中性")]), encoding="utf-8")
    summary = parse_brief(path)
    assert summary.etf_daily_return_pct is None


# ---------------------------------------------------------------------------
# Aggregation — themes
# ---------------------------------------------------------------------------


def test_compose_weekly_digest_detects_5_themes_when_5_recur(week_dir: Path) -> None:
    """新能源汽车, 铝, 铜 (5d each) + 电网 (4d) + AI算力 (3d) = 5 themes."""
    paths = sorted(week_dir.glob("*.md"))
    assert len(paths) == 5
    digest = compose_weekly_digest(paths, anchor=date(2026, 5, 14))
    names = {t.name for t in digest.themes}
    assert names == {"新能源汽车", "铝", "铜", "电网", "AI算力"}
    # 不锈钢 was Mon-only and must be filtered.
    assert "不锈钢" not in names
    # Each theme records the right occurrence count.
    by_name = {t.name: t.occurrence_days for t in digest.themes}
    assert by_name["新能源汽车"] == 5
    assert by_name["铝"] == 5
    assert by_name["铜"] == 5
    assert by_name["电网"] == 4
    assert by_name["AI算力"] == 3


def test_compose_weekly_digest_threshold_can_be_raised(week_dir: Path) -> None:
    """With recurrence_threshold=4 the 3-day themes drop out."""
    paths = sorted(week_dir.glob("*.md"))
    digest = compose_weekly_digest(paths, anchor=date(2026, 5, 14), recurrence_threshold=4)
    names = {t.name for t in digest.themes}
    assert "AI算力" not in names  # 3-day theme removed
    assert "新能源汽车" in names    # 5-day still here


def test_compose_weekly_digest_detects_inflections(week_dir: Path) -> None:
    paths = sorted(week_dir.glob("*.md"))
    digest = compose_weekly_digest(paths, anchor=date(2026, 5, 14))
    # 电网: positive Mon/Tue → negative Thu → positive Fri = 2 flips.
    # 铜:   negative Mon → positive Tue+ = 1 flip.
    names = {i.name for i in digest.inflections}
    assert "电网" in names
    assert "铜" in names
    # 新能源汽车 stays negative all week — must NOT be in the inflection list.
    assert "新能源汽车" not in names


def test_compose_weekly_digest_cumulative_impact_aggregation_correct(week_dir: Path) -> None:
    paths = sorted(week_dir.glob("*.md"))
    digest = compose_weekly_digest(paths, anchor=date(2026, 5, 14))
    cumulative = digest.cumulative_policy_impact
    assert cumulative["新能源汽车"] == pytest.approx(-0.38 + -0.40 + -0.35 + -0.30 + -0.42, abs=1e-6)
    # 电网 spans 4 days only — cumulative is the sum of those 4.
    assert cumulative["电网"] == pytest.approx(0.12 + 0.15 + -0.08 + 0.09, abs=1e-6)
    # AI算力 only on Mon/Wed/Fri.
    assert cumulative["AI算力"] == pytest.approx(0.05 + 0.12 + 0.18, abs=1e-6)


def test_compose_weekly_digest_etf_netflow_is_sum(week_dir: Path) -> None:
    paths = sorted(week_dir.glob("*.md"))
    digest = compose_weekly_digest(paths, anchor=date(2026, 5, 14))
    assert digest.etf_weekly_cumulative_pct == pytest.approx(0.50 + 0.20 + 0.30 + 0.45 + 0.60, abs=1e-6)


# ---------------------------------------------------------------------------
# Empty / degraded paths
# ---------------------------------------------------------------------------


def test_compose_weekly_digest_empty_inputs_returns_notes() -> None:
    digest = compose_weekly_digest([], anchor=date(2026, 5, 14))
    assert digest.brief_count == 0
    assert digest.themes == []
    assert digest.inflections == []
    assert any("0/5" in note for note in digest.notes)
    # Render must succeed on the empty path — empty themes/inflections
    # must NOT raise Jinja's StrictUndefined.
    md = render_weekly_digest_markdown(context=digest.render_context())
    assert "本周回顾" in md
    assert "未达到主题阈值" in md or "本周未达到主题阈值" in md


def test_compose_weekly_digest_skips_unreadable_files(tmp_path: Path) -> None:
    """A file with a non-date stem is skipped (not crash)."""
    briefs = tmp_path / "briefs"
    briefs.mkdir()
    (briefs / "notes.md").write_text("not a brief", encoding="utf-8")
    (briefs / "2026-05-11.md").write_text(
        _brief("2026-05-11", policy=[("新能源汽车", -0.4, 90, "利空")]),
        encoding="utf-8",
    )
    digest = compose_weekly_digest(sorted(briefs.glob("*.md")), anchor=date(2026, 5, 14))
    assert digest.brief_count == 1
    assert any(b.path.name == "2026-05-11.md" for b in digest.briefs_aggregated)


# ---------------------------------------------------------------------------
# Render template
# ---------------------------------------------------------------------------


def test_render_template_contains_expected_sections(week_dir: Path) -> None:
    paths = sorted(week_dir.glob("*.md"))
    digest = compose_weekly_digest(paths, anchor=date(2026, 5, 14))
    md = render_weekly_digest_markdown(context=digest.render_context())
    assert "本周回顾 W20" in md
    assert "2026-05-11 → 2026-05-15" in md
    assert "## 本周核心信号" in md
    assert "## 1. 本周核心主题" in md
    assert "## 2. 信号反转" in md
    assert "## 3. 行业累计影响" in md
    assert "## 4. ETF 资金流摘要" in md
    assert "## 5. 下周展望" in md
    # Every weekly digest must link back to its constituent dailies.
    assert "[2026-05-11.md](../briefs/2026-05-11.md)" in md


def test_render_template_body_uses_beijing_time_for_generated_at(
    week_dir: Path,
) -> None:
    """Regression: ``render/digest.py`` did not register ``beijing_time``,
    so the weekly template's body header rendered the raw ISO Z string
    instead of Beijing wall-clock time. The frontmatter ``generated_at:``
    field stays raw ISO (machine readers parse it).
    """
    paths = sorted(week_dir.glob("*.md"))
    digest = compose_weekly_digest(paths, anchor=date(2026, 5, 14))
    ctx = digest.render_context()
    # Pin a known ISO Z so we can assert the Beijing conversion exactly.
    ctx["fetched_at"] = "2026-05-15T08:59:36Z"
    md = render_weekly_digest_markdown(context=ctx)
    # Body header line must show the formatted Beijing time, not raw ISO.
    assert "2026-05-15 16:59 北京时间" in md
    # And the ISO Z must be absent from the body header line — only the
    # frontmatter ``generated_at:`` key may carry it.
    body_idx = md.find("# 本周回顾")
    assert body_idx > 0
    body = md[body_idx:]
    assert "2026-05-15T08:59:36Z" not in body
    # Frontmatter still has the raw ISO (RSS / Atom consumers need it).
    frontmatter = md[:body_idx]
    assert "generated_at: 2026-05-15T08:59:36Z" in frontmatter


def test_render_template_forecast_flags_persistent_themes(week_dir: Path) -> None:
    paths = sorted(week_dir.glob("*.md"))
    digest = compose_weekly_digest(paths, anchor=date(2026, 5, 14))
    md = render_weekly_digest_markdown(context=digest.render_context())
    # 5-day themes hit the FORECAST_PERSISTENCE_THRESHOLD bar.
    assert FORECAST_PERSISTENCE_THRESHOLD == 4
    assert "本周持续 5 天" in md


# ---------------------------------------------------------------------------
# Detector primitives (defensive)
# ---------------------------------------------------------------------------


def test_detect_themes_below_threshold_returns_empty(week_dir: Path) -> None:
    paths = sorted(week_dir.glob("*.md"))
    summaries = [parse_brief(p) for p in paths]
    themes = _detect_themes(summaries=summaries, kind="policy", recurrence_threshold=10)
    assert themes == []


def test_detect_inflections_flat_signal_does_not_trip(tmp_path: Path) -> None:
    briefs = []
    for date_iso in ("2026-05-11", "2026-05-12", "2026-05-13"):
        path = tmp_path / f"{date_iso}.md"
        # All-zero impact rows — sign() == 0 so no flips expected.
        path.write_text(
            _brief(date_iso, policy=[("电网", 0.0, 1, "中性")]),
            encoding="utf-8",
        )
        briefs.append(parse_brief(path))
    assert _detect_inflections(briefs) == []


# ---------------------------------------------------------------------------
# CLI surface
# ---------------------------------------------------------------------------


def test_cli_weekly_digest_writes_default_filename(
    week_dir: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    digests_dir = tmp_path / "digests"
    rc = cli_main(
        [
            "weekly-digest",
            "--week-of",
            "2026-05-14",
            "--briefs-dir",
            str(week_dir),
            "--digests-dir",
            str(digests_dir),
        ]
    )
    assert rc == 0
    captured = capsys.readouterr().out
    assert "2026-W20" in captured
    written = digests_dir / "2026-W20.md"
    assert written.exists()
    body = written.read_text(encoding="utf-8")
    assert "本周回顾 W20" in body


def test_cli_weekly_digest_with_llm_emits_en_sibling(
    week_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--with-llm`` writes ``.en.md`` even on translator fallback (no API key)."""
    digests_dir = tmp_path / "digests"

    # The translator returns a fallback TranslationResult when no SDK
    # / API key is present — we just need to make sure the .en.md file
    # is written either way.
    from altdata_brief import cli as cli_mod

    def fake_translate(brief_md: str, target_language: str = "en", **kwargs):
        return TranslationResult(
            translated_md="---\nlanguage: en\n---\n# EN digest stub\n",
            source_hash="deadbeef",
            target_language=target_language,
            status="ok",
            model_used="fake-model",
            latency_ms=1.0,
            token_count=(50, 30),
        )

    monkeypatch.setattr(cli_mod, "translate_brief", fake_translate)

    rc = cli_main(
        [
            "weekly-digest",
            "--week-of",
            "2026-05-14",
            "--briefs-dir",
            str(week_dir),
            "--digests-dir",
            str(digests_dir),
            "--with-llm",
            "--llm-usage-log",
            str(tmp_path / "llm_usage.jsonl"),
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "2026-W20.en.md(ok)" in out
    assert (digests_dir / "2026-W20.md").exists()
    assert (digests_dir / "2026-W20.en.md").exists()


# ---------------------------------------------------------------------------
# launchd installer — weekly plist
# ---------------------------------------------------------------------------


def _emit_install_plists(target_dir: Path) -> Path:
    """Render the installer-generated plists with a fake HOME and launchctl."""
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
def test_launchd_weekly_plist_renders_friday_18_00(tmp_path: Path) -> None:
    plist_dir = _emit_install_plists(tmp_path)
    weekly = plist_dir / "com.leonardodon.altdata-brief.weekly.plist"
    assert weekly.exists()
    root = ET.parse(weekly).getroot()
    body = root.find("dict")
    assert body is not None

    # Locate StartCalendarInterval entries.
    children = list(body)
    schedule_array = None
    for idx, child in enumerate(children):
        if child.tag == "key" and child.text == "StartCalendarInterval":
            schedule_array = children[idx + 1]
            break
    assert schedule_array is not None
    entries = schedule_array.findall("dict")
    assert len(entries) == 1, "weekly job should fire on exactly one schedule"
    entry = entries[0]
    kv = {k.text: v.text for k, v in zip(entry.findall("key"), entry.findall("integer"), strict=True)}
    assert kv["Weekday"] == "5"  # Friday
    assert kv["Hour"] == "18"
    assert kv["Minute"] == "0"

    # Must point at the weekly wrapper script.
    args = body.find(".//array")
    assert args is not None
    program_args = [s.text for s in args.findall("string") if s.text]
    assert any("weekly_digest_now.sh" in a for a in program_args)
