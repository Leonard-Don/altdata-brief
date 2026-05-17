"""RSS 2.0 feed generation.

The feed publishes one ``<item>`` per generated brief, capped to the
most-recent ``MAX_ITEMS`` so the file does not grow unbounded across
years of daily output. Std-lib only — no external feed-builder dep.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from email.utils import format_datetime
from pathlib import Path
from xml.etree import ElementTree as ET

CHANNEL_TITLE = "CN AltData Brief"
CHANNEL_DESCRIPTION = (
    "Daily research brief synthesizing alt-data signals from a portfolio of 6 quant projects."
)
CHANNEL_LANGUAGE = "zh-CN"
GENERATOR = "cn-altdata-brief RSS module"

MAX_ITEMS = 50

# Title-line regex: brief.md.j2 always renders "# CN AltData Brief — YYYY-MM-DD".
_TOP_HEADLINE_RE = re.compile(
    r"^- \*\*(?P<name>[^*]+)\*\*[^\n]*",
    flags=re.MULTILINE,
)


def render_feed(
    *,
    briefs_dir: Path,
    feed_path: Path,
    site_url: str = "https://example.github.io/cn-altdata-brief",
    max_items: int = MAX_ITEMS,
    now: datetime | None = None,
) -> Path:
    """Scan ``briefs_dir`` for ``YYYY-MM-DD.md`` files and (over)write ``feed_path``.

    The newest brief becomes the first ``<item>``. ``index.md`` and any
    non-date-named markdown are ignored. Returns ``feed_path`` for chaining.
    """
    feed_path.parent.mkdir(parents=True, exist_ok=True)
    now = now or datetime.now(UTC)

    items = _collect_items(briefs_dir)
    items = items[:max_items]

    rss = ET.Element("rss", attrib={"version": "2.0"})
    channel = ET.SubElement(rss, "channel")
    ET.SubElement(channel, "title").text = CHANNEL_TITLE
    ET.SubElement(channel, "link").text = site_url
    ET.SubElement(channel, "description").text = CHANNEL_DESCRIPTION
    ET.SubElement(channel, "language").text = CHANNEL_LANGUAGE
    ET.SubElement(channel, "generator").text = GENERATOR
    ET.SubElement(channel, "lastBuildDate").text = format_datetime(now)

    for item in items:
        item_el = ET.SubElement(channel, "item")
        ET.SubElement(item_el, "title").text = item["title"]
        ET.SubElement(item_el, "link").text = f"{site_url.rstrip('/')}/briefs/{item['date']}.html"
        ET.SubElement(item_el, "guid", attrib={"isPermaLink": "false"}).text = (
            f"cn-altdata-brief:{item['date']}"
        )
        ET.SubElement(item_el, "pubDate").text = item["pub_date"]
        ET.SubElement(item_el, "description").text = item["description"]

    xml_bytes = ET.tostring(rss, encoding="utf-8", xml_declaration=True)
    feed_path.write_bytes(xml_bytes)
    return feed_path


# ----------------------------------------------------------------------


def _collect_items(briefs_dir: Path) -> list[dict[str, str]]:
    """Return brief descriptors sorted newest first."""
    if not briefs_dir.exists():
        return []
    rows: list[tuple[str, Path]] = []
    for path in briefs_dir.glob("*.md"):
        stem = path.stem
        if not _looks_like_date(stem):
            continue
        rows.append((stem, path))
    rows.sort(reverse=True)
    return [_build_item(stem, path) for stem, path in rows]


def _build_item(date_str: str, path: Path) -> dict[str, str]:
    text = path.read_text(encoding="utf-8")
    headline = _extract_top_headline(text)
    description = _extract_first_paragraph(text)
    title = f"{date_str} · {headline}" if headline else date_str
    return {
        "date": date_str,
        "title": title,
        "pub_date": _date_to_rfc822(date_str),
        "description": description,
    }


def _looks_like_date(stem: str) -> bool:
    if len(stem) != 10 or stem[4] != "-" or stem[7] != "-":
        return False
    try:
        datetime.strptime(stem, "%Y-%m-%d")
    except ValueError:
        return False
    return True


def _date_to_rfc822(date_str: str) -> str:
    # RSS pubDate must be RFC 822; we pin to 09:00 UTC (≈Beijing 17:00 close).
    dt = datetime.strptime(date_str, "%Y-%m-%d").replace(hour=9, tzinfo=UTC)
    return format_datetime(dt)


def _extract_top_headline(brief_md: str) -> str | None:
    """Pick the first bullet's bold-tagged name as the headline hook.

    Falls back to None when the brief is degraded (all sections empty).
    """
    match = _TOP_HEADLINE_RE.search(brief_md)
    if not match:
        return None
    return match.group("name").strip()


def _extract_first_paragraph(brief_md: str) -> str:
    """Return the first non-heading, non-meta paragraph as the RSS description.

    Strips leading metadata blockquote (the auto-generated subtitle) and
    section headings so the description starts with substantive text.
    """
    skip_prefixes = ("#", ">", "---", "![", "**Sources:**", "{%", "<!--")
    for raw in brief_md.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith(skip_prefixes):
            continue
        if line.startswith("- "):
            return line.lstrip("- ").strip()
        return line
    return ""
