"""Tests for the RSS 2.0 feed generator."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from xml.etree import ElementTree as ET

from cn_altdata_brief.render.rss import (
    MAX_ITEMS,
    _build_item,
    _extract_first_paragraph,
    _extract_top_headline,
    _looks_like_date,
    render_feed,
)

SAMPLE_BRIEF = """# CN AltData Brief — 2026-05-17

> 由 `cn-altdata-brief` 在 2026-05-17T01:54:14Z 自动生成。

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
    degraded = "# CN AltData Brief — 2026-05-17\n\n> meta\n\n## 1. 政策动向\n\n_数据缺失_"
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
    assert channel.findtext("title") == "CN AltData Brief"
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
            f"# CN AltData Brief — {date}\n\n- **X**: y\n", encoding="utf-8"
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
    assert channel.findtext("title") == "CN AltData Brief"
    assert channel.findall("item") == []


def test_cli_generate_writes_feed(patched_default_paths: None, tmp_path: Path) -> None:
    from cn_altdata_brief.cli import main

    briefs = tmp_path / "briefs"
    code = main([
        "generate", "--date", "2026-05-17",
        "--briefs-dir", str(briefs), "--charts-dir", str(tmp_path / "charts"),
        "--no-charts", "--site-url", "https://example.com/cn-altdata-brief",
    ])
    assert code == 0
    feed = tmp_path / "feed.xml"
    assert feed.exists()
    item = ET.parse(feed).getroot().find("channel/item")
    assert "2026-05-17" in (item.findtext("title") or "")


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
    assert any("Weekly" in t for t in titles)
    # The digest item must carry a category=weekly-digest.
    categories = [i.findtext("category") for i in items if i.findtext("category")]
    assert "weekly-digest" in categories
    # GUID shape: ``cn-altdata-brief:digest:<stem>``.
    guids = [i.findtext("guid") or "" for i in items]
    assert any("cn-altdata-brief:digest:2026-W20" in g for g in guids)
