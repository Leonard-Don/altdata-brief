"""Tests for the RSS 2.0 feed generator."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from xml.etree import ElementTree as ET

from altdata_brief.render.rss import (
    MAX_ITEMS,
    _build_item,
    _extract_first_paragraph,
    _extract_top_headline,
    _looks_like_date,
    render_feed,
)

SAMPLE_BRIEF = """# AltData Brief — 2026-05-17

> 由 `altdata-brief` 在 2026-05-17T01:54:14Z 自动生成。

---

## 1. 政策动向

- **新能源汽车**：avg_impact=-0.388 (负向) · mentions=94 · 信号=利空
- **电网**：avg_impact=+0.100 (正向) · mentions=8
"""


def test_helpers() -> None:
    assert _looks_like_date("2026-05-17")
    assert not _looks_like_date("index")
    assert not _looks_like_date("2026-13-99")
    assert _extract_top_headline(SAMPLE_BRIEF) == "新能源汽车"
    degraded = "# AltData Brief — 2026-05-17\n\n> meta\n\n## 1. 政策动向\n\n_数据缺失_"
    assert _extract_top_headline(degraded) is None
    assert "新能源汽车" in _extract_first_paragraph(SAMPLE_BRIEF)


def test_build_item_shape(tmp_path: Path) -> None:
    brief = tmp_path / "2026-05-17.md"
    brief.write_text(SAMPLE_BRIEF, encoding="utf-8")
    item = _build_item("2026-05-17", brief)
    assert item["date"] == "2026-05-17"
    assert "新能源汽车" in item["title"]
    assert "2026" in item["pub_date"]


def test_render_feed_creates_valid_rss(tmp_path: Path) -> None:
    briefs = tmp_path / "briefs"
    briefs.mkdir()
    (briefs / "2026-05-17.md").write_text(SAMPLE_BRIEF, encoding="utf-8")
    (briefs / "2026-05-16.md").write_text(
        SAMPLE_BRIEF.replace("2026-05-17", "2026-05-16"), encoding="utf-8"
    )
    (briefs / "index.md").write_text("# index — should be ignored", encoding="utf-8")

    feed_path = tmp_path / "feed.xml"
    render_feed(briefs_dir=briefs, feed_path=feed_path, site_url="https://example.com")

    root = ET.parse(feed_path).getroot()
    assert root.tag == "rss"
    assert root.attrib["version"] == "2.0"
    channel = root.find("channel")
    assert channel.findtext("title") == "多市场另类数据日报"
    assert channel.findtext("link") == "https://example.com"

    items = channel.findall("item")
    assert len(items) == 2  # index.md skipped
    assert "2026-05-17" in items[0].findtext("title")
    assert "2026-05-16" in items[1].findtext("title")
    for item in items:
        assert item.findtext("title")
        assert item.findtext("link", "").startswith("https://example.com/briefs/")
        assert item.findtext("guid")
        assert item.findtext("pubDate")
        assert item.findtext("description")


def test_render_feed_caps_items(tmp_path: Path) -> None:
    briefs = tmp_path / "briefs"
    briefs.mkdir()
    # 60 chronologically-valid briefs.
    base = datetime(2026, 1, 1, tzinfo=UTC)
    for i in range(60):
        date = (base + timedelta(days=i)).strftime("%Y-%m-%d")
        (briefs / f"{date}.md").write_text(
            f"# AltData Brief — {date}\n\n- **X**: y\n", encoding="utf-8"
        )
    feed_path = tmp_path / "feed.xml"
    render_feed(briefs_dir=briefs, feed_path=feed_path)
    items = ET.parse(feed_path).getroot().find("channel").findall("item")
    assert len(items) == MAX_ITEMS


def test_render_feed_empty_dir_emits_channel(tmp_path: Path) -> None:
    feed_path = tmp_path / "feed.xml"
    render_feed(
        briefs_dir=tmp_path / "empty",
        feed_path=feed_path,
        now=datetime(2026, 5, 17, tzinfo=UTC),
    )
    channel = ET.parse(feed_path).getroot().find("channel")
    assert channel.findtext("title") == "多市场另类数据日报"
    assert channel.findall("item") == []


def test_cli_generate_writes_feed(patched_default_paths: None, tmp_path: Path) -> None:
    from altdata_brief.cli import main

    briefs = tmp_path / "briefs"
    code = main([
        "generate", "--date", "2026-05-17",
        "--briefs-dir", str(briefs), "--charts-dir", str(tmp_path / "charts"),
        "--no-charts", "--site-url", "https://example.com/altdata-brief",
    ])
    assert code == 0
    feed = tmp_path / "feed.xml"
    assert feed.exists()
    item = ET.parse(feed).getroot().find("channel/item")
    assert "2026-05-17" in (item.findtext("title") or "")


def test_cli_generate_uses_real_default_site_url(
    patched_default_paths: None, tmp_path: Path
) -> None:
    from altdata_brief.cli import DEFAULT_SITE_URL, main

    briefs = tmp_path / "briefs"
    code = main(
        [
            "generate",
            "--date",
            "2026-05-17",
            "--briefs-dir",
            str(briefs),
            "--charts-dir",
            str(tmp_path / "charts"),
            "--no-charts",
        ]
    )
    assert code == 0
    feed = tmp_path / "feed.xml"
    root = ET.parse(feed).getroot()
    channel = root.find("channel")
    assert channel.findtext("link") == DEFAULT_SITE_URL
    assert channel.findtext("item/link", "").startswith(f"{DEFAULT_SITE_URL}/briefs/")
    brief_text = (briefs / "2026-05-17.md").read_text(encoding="utf-8")
    assert DEFAULT_SITE_URL in brief_text
    assert "example.github.io" not in feed.read_text(encoding="utf-8")
    assert "example.github.io" not in brief_text


def test_render_feed_merges_weekly_digests(tmp_path: Path) -> None:
    """v0.9 — when ``digests_dir`` is supplied, weekly digests merge into the same feed."""
    briefs = tmp_path / "briefs"
    briefs.mkdir()
    (briefs / "2026-05-15.md").write_text(
        SAMPLE_BRIEF.replace("2026-05-17", "2026-05-15"), encoding="utf-8"
    )
    digests = tmp_path / "digests"
    digests.mkdir()
    (digests / "2026-W20.md").write_text(
        "# 本周回顾 W20 — 2026-05-11 → 2026-05-15\n\n- **新能源汽车**: weekly takeaway\n",
        encoding="utf-8",
    )
    feed_path = tmp_path / "feed.xml"
    render_feed(
        briefs_dir=briefs,
        feed_path=feed_path,
        digests_dir=digests,
        site_url="https://example.com",
    )
    root = ET.parse(feed_path).getroot()
    items = root.findall("channel/item")
    titles = [i.findtext("title") or "" for i in items]
    # Daily AND weekly items must both appear.
    assert any("2026-05-15" in t for t in titles)
    assert any("周报" in t for t in titles)
    # The digest item must carry a category=weekly-digest.
    categories = [i.findtext("category") for i in items if i.findtext("category")]
    assert "weekly-digest" in categories
    # GUID shape: ``altdata-brief:digest:<stem>``.
    guids = [i.findtext("guid") or "" for i in items]
    assert any("altdata-brief:digest:2026-W20" in g for g in guids)


# ---------------------------------------------------------------------------
# Markdown-emphasis strip regression — RSS / Atom feed readers render the
# ``<description>`` and ``<summary>`` text verbatim. Leaving the raw
# ``**bold**`` markers in means feed clients literally show the asterisks
# in the preview pane. The fix routes ``_extract_first_paragraph`` output
# through :func:`og_metadata._strip_md_emphasis` so feed-facing prose
# stays clean.
# ---------------------------------------------------------------------------


def test_extract_first_paragraph_strips_markdown_emphasis() -> None:
    """Unit-level: emphasis markers are stripped from feed prose."""
    bold = "- **AI算力**: 政策影响=+1.000"
    inline = "- 今日核心信号是 *煤炭开采加工* 与 `super-pricing`"
    assert _extract_first_paragraph(bold) == "AI算力: 政策影响=+1.000"
    assert _extract_first_paragraph(inline) == (
        "今日核心信号是 煤炭开采加工 与 super-pricing"
    )


def test_render_feed_descriptions_have_no_markdown_emphasis_markers(
    tmp_path: Path,
) -> None:
    """RSS feed.xml: no ``<description>`` text contains ``**`` markers."""
    briefs = tmp_path / "briefs"
    briefs.mkdir()
    (briefs / "2026-05-17.md").write_text(SAMPLE_BRIEF, encoding="utf-8")
    (briefs / "2026-05-16.md").write_text(
        SAMPLE_BRIEF.replace("2026-05-17", "2026-05-16"), encoding="utf-8"
    )
    digests = tmp_path / "digests"
    digests.mkdir()
    (digests / "2026-W20.md").write_text(
        "# 本周回顾 W20 — 2026-05-11 → 2026-05-15\n\n"
        "- **新能源汽车**: weekly takeaway with __underscore_bold__ too\n",
        encoding="utf-8",
    )

    feed_path = tmp_path / "feed.xml"
    render_feed(
        briefs_dir=briefs,
        feed_path=feed_path,
        digests_dir=digests,
        site_url="https://example.com",
    )
    root = ET.parse(feed_path).getroot()
    for desc in root.findall("channel/item/description"):
        text = desc.text or ""
        assert "**" not in text, f"feed.xml description still contains **: {text!r}"
        assert "__" not in text, f"feed.xml description still contains __: {text!r}"


def test_render_atom_feed_summary_and_content_have_no_emphasis_markers(
    tmp_path: Path,
) -> None:
    """Atom feed.atom: no <summary> or <content> text contains ``**``."""
    from altdata_brief.render.rss import render_atom_feed

    briefs = tmp_path / "briefs"
    briefs.mkdir()
    (briefs / "2026-05-17.md").write_text(SAMPLE_BRIEF, encoding="utf-8")
    atom_path = tmp_path / "feed.atom"
    render_atom_feed(
        briefs_dir=briefs,
        feed_path=atom_path,
        site_url="https://example.com",
    )
    raw = atom_path.read_text(encoding="utf-8")
    ns = "{http://www.w3.org/2005/Atom}"
    root = ET.parse(atom_path).getroot()
    for entry in root.findall(f"{ns}entry"):
        summary = (entry.findtext(f"{ns}summary") or "")
        # Atom <content> for our briefs is HTML-wrapped via CDATA; parse
        # the on-disk text to see the CDATA payload directly.
        assert "**" not in summary, f"atom <summary> still has **: {summary!r}"
    # The CDATA body in raw XML must not contain literal ``**`` either.
    assert "<![CDATA[<p>**" not in raw
    # Even with underscore-bold input the marker must not leak.
    assert "__新能源" not in raw


def test_atom_date_from_brief_date_handles_all_cadences() -> None:
    """Daily / weekly / monthly stems each map to a distinct Atom stamp."""
    from altdata_brief.render.rss import _atom_date_from_brief_date

    # Daily — pinned to 09:00 UTC.
    assert _atom_date_from_brief_date("2026-05-20") == "2026-05-20T09:00:00Z"
    # Weekly digest — the Friday of that ISO week, 18:00 UTC.
    assert _atom_date_from_brief_date("2026-W20") == "2026-05-15T18:00:00Z"
    # Monthly digest — the last day of the month, 17:00 UTC.
    assert _atom_date_from_brief_date("2026-04") == "2026-04-30T17:00:00Z"
    # December monthly exercises the month+1 → next-year rollover.
    assert _atom_date_from_brief_date("2026-12") == "2026-12-31T17:00:00Z"
    # An unrecognized stem falls back to a valid (never-empty) stamp.
    fallback = _atom_date_from_brief_date("not-a-stem")
    assert "T" in fallback and fallback.endswith("Z")


def test_render_feed_merges_monthly_digests(tmp_path: Path) -> None:
    """v0.11 — monthly digests merge into the same RSS feed as dailies/weeklies."""
    briefs = tmp_path / "briefs"
    briefs.mkdir()
    (briefs / "2026-05-15.md").write_text(
        SAMPLE_BRIEF.replace("2026-05-17", "2026-05-15"), encoding="utf-8"
    )
    digests = tmp_path / "digests"
    digests.mkdir()
    (digests / "2026-04.md").write_text(
        "# 上月回顾 2026-04\n\n- **新能源汽车**: monthly takeaway\n", encoding="utf-8"
    )
    # December stem too — exercises the month+1 last-day rollover.
    (digests / "2026-12.md").write_text(
        "# 上月回顾 2026-12\n\n- **电网**: year-end takeaway\n", encoding="utf-8"
    )
    feed_path = tmp_path / "feed.xml"
    render_feed(
        briefs_dir=briefs,
        feed_path=feed_path,
        digests_dir=digests,
        site_url="https://example.com",
    )
    root = ET.parse(feed_path).getroot()
    items = root.findall("channel/item")
    titles = [i.findtext("title") or "" for i in items]
    assert any("2026-05-15" in t for t in titles)
    assert any("月报" in t for t in titles)
    categories = [i.findtext("category") for i in items if i.findtext("category")]
    assert "monthly-digest" in categories
    guids = [i.findtext("guid") or "" for i in items]
    assert any("2026-04" in g for g in guids)
    assert any("2026-12" in g for g in guids)


def test_render_atom_feed_includes_weekly_and_monthly_digests(tmp_path: Path) -> None:
    """v0.11 — the Atom feed carries weekly and monthly digest entries."""
    from altdata_brief.render.rss import render_atom_feed

    briefs = tmp_path / "briefs"
    briefs.mkdir()
    (briefs / "2026-05-15.md").write_text(
        SAMPLE_BRIEF.replace("2026-05-17", "2026-05-15"), encoding="utf-8"
    )
    digests = tmp_path / "digests"
    digests.mkdir()
    (digests / "2026-W20.md").write_text(
        "# 本周回顾 W20\n\n- **新能源汽车**: weekly takeaway\n", encoding="utf-8"
    )
    (digests / "2026-04.md").write_text(
        "# 上月回顾 2026-04\n\n- **电网**: monthly takeaway\n", encoding="utf-8"
    )
    atom_path = tmp_path / "feed.atom"
    render_atom_feed(
        briefs_dir=briefs,
        feed_path=atom_path,
        digests_dir=digests,
        site_url="https://example.com",
    )
    ns = "{http://www.w3.org/2005/Atom}"
    root = ET.parse(atom_path).getroot()
    entries = root.findall(f"{ns}entry")
    titles = [e.findtext(f"{ns}title") or "" for e in entries]
    assert any("2026-05-15" in t for t in titles)
    assert any("周报" in t for t in titles)
    assert any("月报" in t for t in titles)
    # Every entry carries a non-empty, Z-suffixed updated timestamp.
    for entry in entries:
        assert (entry.findtext(f"{ns}updated") or "").endswith("Z")
