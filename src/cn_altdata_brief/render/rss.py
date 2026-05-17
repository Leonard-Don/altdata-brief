"""RSS 2.0 feed generation.

The feed publishes one ``<item>`` per generated brief, capped to the
most-recent ``MAX_ITEMS`` so the file does not grow unbounded across
years of daily output. Std-lib only — no external feed-builder dep.

v0.8: bilingual support. Each date that has both a CN brief
(``YYYY-MM-DD.md``) and an EN brief (``YYYY-MM-DD.en.md``) yields
**two** ``<item>`` elements — the EN one is title-prefixed ``[EN]`` and
points at ``YYYY-MM-DD.en.html``. Subscribers can filter by GUID
suffix (``:en``) if they only want one language.
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
# Channel-level language stays zh-CN because Chinese is the ground
# truth; per-item ``<language>`` elements override for EN entries.
CHANNEL_LANGUAGE = "zh-CN"
GENERATOR = "cn-altdata-brief RSS module"

MAX_ITEMS = 50

# Title-line regex: brief.md.j2 always renders "# CN AltData Brief — YYYY-MM-DD".
_TOP_HEADLINE_RE = re.compile(
    r"^- \*\*(?P<name>[^*]+)\*\*[^\n]*",
    flags=re.MULTILINE,
)

# Suffix → (item_language, title_prefix, description_fallback). Order
# matters: CN first so it occupies the more prominent slot per date.
_LANGUAGE_VARIANTS: tuple[tuple[str, str, str, str], ...] = (
    ("", "zh-CN", "", ""),
    (".en", "en", "[EN] ", "English translation — see Chinese version for ground truth."),
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
        suffix = item.get("file_suffix", "")
        ET.SubElement(item_el, "link").text = (
            f"{site_url.rstrip('/')}/briefs/{item['date']}{suffix}.html"
        )
        guid_lang = item.get("guid_lang") or ""
        guid_tail = f":{guid_lang}" if guid_lang else ""
        ET.SubElement(item_el, "guid", attrib={"isPermaLink": "false"}).text = (
            f"cn-altdata-brief:{item['date']}{guid_tail}"
        )
        ET.SubElement(item_el, "pubDate").text = item["pub_date"]
        ET.SubElement(item_el, "description").text = item["description"]
        # Per-item language overrides the channel-level zh-CN default
        # so feed readers can filter on RFC 5646 language tags.
        ET.SubElement(item_el, "language").text = item.get("language", CHANNEL_LANGUAGE)

    ET.indent(rss, space="  ", level=0)  # human-readable pretty-print
    xml_bytes = ET.tostring(rss, encoding="utf-8", xml_declaration=True)
    feed_path.write_bytes(xml_bytes)
    return feed_path


# ----------------------------------------------------------------------


def _collect_items(briefs_dir: Path) -> list[dict[str, str]]:
    """Return brief descriptors sorted newest first.

    Iterates dated CN briefs first, then for each date appends the
    matching ``.en.md`` (and any future language siblings) so feed
    items are grouped per-date but always lead with the CN ground
    truth.
    """
    if not briefs_dir.exists():
        return []
    cn_dates: list[str] = []
    for path in briefs_dir.glob("*.md"):
        stem = path.stem
        if "." in stem:  # skip 2026-05-17.en — we'll find them via the date
            continue
        if not _looks_like_date(stem):
            continue
        cn_dates.append(stem)
    cn_dates.sort(reverse=True)

    items: list[dict[str, str]] = []
    for date_str in cn_dates:
        for suffix, lang, title_prefix, desc_fallback in _LANGUAGE_VARIANTS:
            candidate = briefs_dir / f"{date_str}{suffix}.md"
            if not candidate.exists():
                continue
            items.append(
                _build_item(
                    date_str,
                    candidate,
                    file_suffix=suffix,
                    language=lang,
                    title_prefix=title_prefix,
                    description_fallback=desc_fallback,
                )
            )
    return items


def _build_item(
    date_str: str,
    path: Path,
    *,
    file_suffix: str = "",
    language: str = CHANNEL_LANGUAGE,
    title_prefix: str = "",
    description_fallback: str = "",
) -> dict[str, str]:
    text = path.read_text(encoding="utf-8")
    headline = _extract_top_headline(text)
    description = _extract_first_paragraph(text) or description_fallback
    base_title = f"{date_str} · {headline}" if headline else date_str
    title = f"{title_prefix}{base_title}"
    guid_lang = language.split("-")[0] if language and language != CHANNEL_LANGUAGE else ""
    return {
        "date": date_str,
        "title": title,
        "pub_date": _date_to_rfc822(date_str),
        "description": description,
        "file_suffix": file_suffix,
        "language": language,
        "guid_lang": guid_lang,
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
