"""RSS 2.0 feed generation.

The feed publishes one ``<item>`` per generated brief, capped to the
most-recent ``MAX_ITEMS`` so the file does not grow unbounded across
years of daily output. Std-lib only — no external feed-builder dep.

v0.8: bilingual support. Each date that has both a CN brief
(``YYYY-MM-DD.md``) and an EN brief (``YYYY-MM-DD.en.md``) yields
**two** ``<item>`` elements — the EN one is title-prefixed ``[EN]`` and
points at ``YYYY-MM-DD.en.html``. Subscribers can filter by GUID
suffix (``:en``) if they only want one language.

v0.10: enrichment — each ``<item>`` now carries an ``<enclosure>`` for
the brief's OG image (RSS-standard podcast-style attachment, rendered
as a thumbnail in modern readers), one or more ``<category>`` tags
extracted from the brief's signal industries, and a CDATA-wrapped HTML
``<description>`` containing the markdown preview. A parallel Atom 1.0
feed is emitted as ``feed.atom`` for clients that prefer it over RSS
2.0 (notably Inoreader, Feedly's newer parser, and most mail-based
readers).
"""

from __future__ import annotations

import html
import re
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from pathlib import Path
from uuid import uuid4
from xml.etree import ElementTree as ET

CHANNEL_TITLE = "多市场另类数据日报"
CHANNEL_DESCRIPTION = (
    "基于 4 个公开摘要/快照数据源合成的多市场另类数据研究简报。"
)
DEFAULT_SITE_URL = "https://leonard-don.github.io/altdata-brief"
# Channel-level language stays zh-CN because Chinese is the ground
# truth; per-item ``<language>`` elements override for EN entries.
CHANNEL_LANGUAGE = "zh-CN"
GENERATOR = "altdata-brief RSS module"

MAX_ITEMS = 50

# Title-line regex: brief.md.j2 always renders "# AltData Brief — YYYY-MM-DD".
_TOP_HEADLINE_RE = re.compile(
    r"^- \*\*(?P<name>[^*]+)\*\*[^\n]*",
    flags=re.MULTILINE,
)

# Suffix → (item_language, title_prefix, description_fallback). Order
# matters: CN first so it occupies the more prominent slot per date.
_LANGUAGE_VARIANTS: tuple[tuple[str, str, str, str], ...] = (
    ("", "zh-CN", "", ""),
    (".en", "en", "[EN] ", "英文翻译暂不可用，请以中文版本为准。"),
)


def render_feed(
    *,
    briefs_dir: Path,
    feed_path: Path,
    site_url: str = DEFAULT_SITE_URL,
    max_items: int = MAX_ITEMS,
    now: datetime | None = None,
    digests_dir: Path | None = None,
    chart_dir: Path | None = None,
    sections_by_date: dict[str, dict] | None = None,
) -> Path:
    """Scan ``briefs_dir`` for ``YYYY-MM-DD.md`` files and (over)write ``feed_path``.

    The newest brief becomes the first ``<item>``. ``index.md`` and any
    non-date-named markdown are ignored. Returns ``feed_path`` for chaining.

    v0.9 — when ``digests_dir`` is provided and exists, weekly digests
    are merged into the same feed with a ``[Weekly]`` title prefix and
    a ``:digest`` GUID suffix so subscribers can filter on cadence as
    well as language.
    """
    feed_path.parent.mkdir(parents=True, exist_ok=True)
    now = now or datetime.now(UTC)

    items = _collect_items(
        briefs_dir, chart_dir=chart_dir, sections_by_date=sections_by_date, site_url=site_url
    )
    if digests_dir is not None and digests_dir.exists():
        items.extend(
            _collect_digest_items(
                digests_dir, chart_dir=chart_dir, site_url=site_url
            )
        )
        items.extend(
            _collect_monthly_items(
                digests_dir, chart_dir=chart_dir, site_url=site_url
            )
        )
    items.sort(key=lambda it: it.get("sort_key", ""), reverse=True)
    items = items[:max_items]

    rss = ET.Element("rss", attrib={"version": "2.0"})
    channel = ET.SubElement(rss, "channel")
    ET.SubElement(channel, "title").text = CHANNEL_TITLE
    ET.SubElement(channel, "link").text = site_url
    ET.SubElement(channel, "description").text = CHANNEL_DESCRIPTION
    ET.SubElement(channel, "language").text = CHANNEL_LANGUAGE
    ET.SubElement(channel, "generator").text = GENERATOR
    ET.SubElement(channel, "lastBuildDate").text = format_datetime(now)

    cdata_blocks: dict[str, str] = {}
    for item in items:
        item_el = ET.SubElement(channel, "item")
        ET.SubElement(item_el, "title").text = item["title"]
        suffix = item.get("file_suffix", "")
        kind = item.get("kind", "brief")
        if kind == "digest":
            link_path = f"digests/{item['date']}{suffix}.html"
            guid_prefix = "altdata-brief:digest"
        elif kind == "monthly":
            link_path = f"digests/{item['date']}{suffix}.html"
            guid_prefix = "altdata-brief:monthly"
        else:
            link_path = f"briefs/{item['date']}{suffix}.html"
            guid_prefix = "altdata-brief"
        ET.SubElement(item_el, "link").text = (
            f"{site_url.rstrip('/')}/{link_path}"
        )
        guid_lang = item.get("guid_lang") or ""
        guid_tail = f":{guid_lang}" if guid_lang else ""
        ET.SubElement(item_el, "guid", attrib={"isPermaLink": "false"}).text = (
            f"{guid_prefix}:{item['date']}{guid_tail}"
        )
        ET.SubElement(item_el, "pubDate").text = item["pub_date"]
        desc_el = ET.SubElement(item_el, "description")
        _set_cdata_text(
            desc_el,
            _escape_feed_html_text(item["description"]),
            cdata_blocks,
        )
        # Per-item language overrides the channel-level zh-CN default
        # so feed readers can filter on RFC 5646 language tags.
        ET.SubElement(item_el, "language").text = item.get("language", CHANNEL_LANGUAGE)
        if kind == "digest":
            # Custom category so subscribers can filter weekly vs daily
            # without parsing the title prefix.
            ET.SubElement(item_el, "category").text = "weekly-digest"
        elif kind == "monthly":
            # v0.11 — monthly cadence flag for subscriber filters.
            ET.SubElement(item_el, "category").text = "monthly-digest"
        # v0.10 — semantic categories from the brief's signal industries.
        for cat in item.get("categories") or ():
            ET.SubElement(item_el, "category").text = cat
        # v0.10 — enclosure for the OG image. RSS 2.0 spec requires
        # ``url``, ``length`` (size in bytes; ``0`` is tolerated when
        # unknown — we don't HEAD the chart over the network), and
        # ``type`` (MIME).
        image_url = item.get("image_url")
        if image_url:
            ET.SubElement(
                item_el,
                "enclosure",
                attrib={
                    "url": image_url,
                    "length": str(item.get("image_length", 0)),
                    "type": "image/png",
                },
            )

    _write_xml_with_cdata(feed_path, rss, cdata_blocks)
    return feed_path


# ----------------------------------------------------------------------


def _collect_items(
    briefs_dir: Path,
    *,
    chart_dir: Path | None = None,
    sections_by_date: dict[str, dict] | None = None,
    site_url: str = "",
) -> list[dict[str, str]]:
    """Return brief descriptors sorted newest first.

    Iterates dated CN briefs first, then for each date appends the
    matching ``.en.md`` (and any future language siblings) so feed
    items are grouped per-date but always lead with the CN ground
    truth.

    v0.10 — when ``chart_dir`` and ``sections_by_date`` are supplied,
    each item is enriched with the OG image URL + signal categories
    (driving the RSS ``<enclosure>`` / ``<category>`` extensions).
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
        date_chart_dir = (chart_dir / date_str) if chart_dir else None
        date_sections = (sections_by_date or {}).get(date_str)
        for suffix, lang, title_prefix, desc_fallback in _LANGUAGE_VARIANTS:
            candidate = briefs_dir / f"{date_str}{suffix}.md"
            if not candidate.exists():
                continue
            item = _build_item(
                date_str,
                candidate,
                file_suffix=suffix,
                language=lang,
                title_prefix=title_prefix,
                description_fallback=desc_fallback,
            )
            _enrich_item(
                item,
                date_str,
                chart_dir=date_chart_dir,
                sections=date_sections,
                site_url=site_url,
            )
            items.append(item)
    return items


def _enrich_item(
    item: dict[str, str],
    date_str: str,
    *,
    chart_dir: Path | None,
    sections: dict | None,
    site_url: str,
) -> None:
    """v0.10 — attach OG image URL + categories to a feed item dict.

    Imported lazily inside the function so this module stays importable
    even before publish/og_metadata is available (defensive — avoids a
    circular import if someone ever depends on render.rss from publish).
    """
    from altdata_brief.publish.og_metadata import signal_categories_for
    from altdata_brief.render.og_image import chart_url_for, pick_og_chart

    chart_key, chart_path = pick_og_chart(sections, chart_dir)
    if site_url and chart_key and chart_path is not None and chart_path.exists():
        item["image_url"] = chart_url_for(chart_key, date_str, site_url=site_url)
        try:
            item["image_length"] = chart_path.stat().st_size
        except OSError:
            item["image_length"] = 0
    cats = signal_categories_for(sections)
    if cats:
        item["categories"] = cats


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
        "kind": "brief",
        # ISO-8601 date sorts lexicographically — used by render_feed
        # to merge daily + weekly items into a single newest-first list.
        "sort_key": date_str,
    }


def _collect_digest_items(
    digests_dir: Path,
    *,
    chart_dir: Path | None = None,
    site_url: str = "",
) -> list[dict[str, str]]:
    """Return RSS items for every weekly digest under ``digests_dir``.

    The digest filename convention is ``<iso_year>-W<week>.md`` (and
    optional ``.en`` sibling). We synthesize a sortable date from the
    Friday of that ISO week so the merged feed orders daily + weekly
    items chronologically.
    """
    if not digests_dir.exists():
        return []
    cn_stems: list[tuple[str, str]] = []
    digest_re = __import__("re").compile(r"^(\d{4})-W(\d{2})$")
    for path in digests_dir.glob("*.md"):
        stem = path.stem
        if "." in stem:
            continue
        m = digest_re.fullmatch(stem)
        if not m:
            continue
        iso_year = int(m.group(1))
        iso_week = int(m.group(2))
        friday = _iso_week_friday(iso_year, iso_week)
        cn_stems.append((stem, friday.isoformat()))
    cn_stems.sort(key=lambda kv: kv[1], reverse=True)

    items: list[dict[str, str]] = []
    for stem, friday_iso in cn_stems:
        for suffix, lang, title_prefix, desc_fallback in _LANGUAGE_VARIANTS:
            candidate = digests_dir / f"{stem}{suffix}.md"
            if not candidate.exists():
                continue
            text = candidate.read_text(encoding="utf-8")
            headline = _extract_top_headline(text)
            description = _extract_first_paragraph(text) or desc_fallback
            base_title = f"本周回顾 {stem}" if not title_prefix else f"Weekly Digest {stem}"
            title = f"[周报] {base_title}" if not title_prefix else f"[Weekly] {base_title}"
            if headline:
                title = f"{title} · {headline}"
            if title_prefix:
                title = f"{title_prefix.strip()} {title}".strip()
            guid_lang = lang.split("-")[0] if lang and lang != CHANNEL_LANGUAGE else ""
            items.append(
                {
                    "date": stem,
                    "title": title,
                    "pub_date": _date_to_rfc822(friday_iso),
                    "description": description,
                    "file_suffix": suffix,
                    "language": lang,
                    "guid_lang": guid_lang,
                    "kind": "digest",
                    "sort_key": friday_iso,
                }
            )
    return items


def _collect_monthly_items(
    digests_dir: Path,
    *,
    chart_dir: Path | None = None,
    site_url: str = "",
) -> list[dict[str, str]]:
    """v0.11 — return RSS items for every monthly digest under ``digests_dir``.

    Monthly digests share ``digests/`` with weekly digests; the
    filename shape (``YYYY-MM`` vs ``YYYY-Www``) distinguishes the
    cadence. Sort key is the last day of the month so the merged feed
    orders daily / weekly / monthly items chronologically.
    """
    if not digests_dir.exists():
        return []
    cn_stems: list[tuple[str, str]] = []
    monthly_re = re.compile(r"^(\d{4})-(\d{2})$")
    for path in digests_dir.glob("*.md"):
        stem = path.stem
        if "." in stem:
            continue
        m = monthly_re.fullmatch(stem)
        if not m:
            continue
        year = int(m.group(1))
        month = int(m.group(2))
        if not 1 <= month <= 12:
            continue
        # Last day of the month — calendar.monthrange would import
        # another stdlib; the next-month-day-zero trick is cheaper.
        if month == 12:
            last_day = (datetime(year + 1, 1, 1) - timedelta(days=1)).date()
        else:
            last_day = (datetime(year, month + 1, 1) - timedelta(days=1)).date()
        cn_stems.append((stem, last_day.isoformat()))
    cn_stems.sort(key=lambda kv: kv[1], reverse=True)

    items: list[dict[str, str]] = []
    for stem, last_iso in cn_stems:
        for suffix, lang, title_prefix, desc_fallback in _LANGUAGE_VARIANTS:
            candidate = digests_dir / f"{stem}{suffix}.md"
            if not candidate.exists():
                continue
            text = candidate.read_text(encoding="utf-8")
            headline = _extract_top_headline(text)
            description = _extract_first_paragraph(text) or desc_fallback
            base_title = (
                f"上月回顾 {stem}" if not title_prefix else f"Monthly Digest {stem}"
            )
            title = f"[月报] {base_title}" if not title_prefix else f"[Monthly] {base_title}"
            if headline:
                title = f"{title} · {headline}"
            if title_prefix:
                title = f"{title_prefix.strip()} {title}".strip()
            guid_lang = lang.split("-")[0] if lang and lang != CHANNEL_LANGUAGE else ""
            items.append(
                {
                    "date": stem,
                    "title": title,
                    "pub_date": _date_to_rfc822(last_iso),
                    "description": description,
                    "file_suffix": suffix,
                    "language": lang,
                    "guid_lang": guid_lang,
                    "kind": "monthly",
                    "sort_key": last_iso,
                }
            )
    return items


def _iso_week_friday(iso_year: int, iso_week: int):
    """Return the Friday of the given ISO year/week as a date object.

    We anchor weekly digest pub dates to the Friday so they ship after
    the week's last daily brief — feed readers see them as "the most
    recent item this Friday".
    """
    from datetime import date, timedelta

    # ISO date of the Monday of the requested week.
    jan4 = date(iso_year, 1, 4)
    iso_year_monday1 = jan4 - timedelta(days=jan4.isoweekday() - 1)
    monday = iso_year_monday1 + timedelta(weeks=iso_week - 1)
    return monday + timedelta(days=4)


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

    Strips leading YAML frontmatter (``---\\n…\\n---``), metadata
    blockquote (the auto-generated subtitle), and section headings so
    the description starts with substantive text. Markdown emphasis
    (``**bold**`` / ``*italic*`` / ``__bold__`` / `` `code` ``) is
    stripped so feed readers don't render the literal asterisks — the
    same idempotent stripper used by OG/Twitter Card descriptions.
    """
    # Lazy import — keeps render.rss importable in isolation (avoids a
    # potential circular import if publish ever depends on render.rss).
    from altdata_brief.publish.og_metadata import _strip_md_emphasis

    skip_prefixes = ("#", ">", "---", "![", "**Sources:**", "**来源：**", "{%", "<!--")
    lines = brief_md.splitlines()
    start = 0
    if lines and lines[0].strip() == "---":
        for i, raw in enumerate(lines[1:], start=1):
            if raw.strip() == "---":
                start = i + 1
                break
    for raw in lines[start:]:
        line = raw.strip()
        if not line:
            continue
        if line.startswith(skip_prefixes):
            continue
        if line.startswith("- "):
            return _strip_md_emphasis(line.lstrip("- ").strip())
        return _strip_md_emphasis(line)
    return ""


# ---------------------------------------------------------------------------
# v0.10 — Atom 1.0 feed
# ---------------------------------------------------------------------------


ATOM_NS = "http://www.w3.org/2005/Atom"


def render_atom_feed(
    *,
    briefs_dir: Path,
    feed_path: Path,
    site_url: str = DEFAULT_SITE_URL,
    max_items: int = MAX_ITEMS,
    now: datetime | None = None,
    digests_dir: Path | None = None,
    chart_dir: Path | None = None,
    sections_by_date: dict[str, dict] | None = None,
) -> Path:
    """Emit an Atom 1.0 feed mirroring the RSS 2.0 contents.

    Atom is more strictly specified than RSS — every entry needs a
    globally-unique ``<id>`` (we reuse the RSS GUID), an ``<updated>``
    timestamp, and one ``<author>``. Modern feed clients prefer Atom
    when both are advertised because the schema is unambiguous.

    Same parameter shape as :func:`render_feed` so the publisher can
    invoke both side-by-side without rebuilding state.
    """
    feed_path.parent.mkdir(parents=True, exist_ok=True)
    now = now or datetime.now(UTC)

    items = _collect_items(
        briefs_dir,
        chart_dir=chart_dir,
        sections_by_date=sections_by_date,
        site_url=site_url,
    )
    if digests_dir is not None and digests_dir.exists():
        items.extend(
            _collect_digest_items(
                digests_dir, chart_dir=chart_dir, site_url=site_url
            )
        )
        items.extend(
            _collect_monthly_items(
                digests_dir, chart_dir=chart_dir, site_url=site_url
            )
        )
    items.sort(key=lambda it: it.get("sort_key", ""), reverse=True)
    items = items[:max_items]

    # Build the Atom XML using ElementTree with the Atom namespace as the
    # default xmlns — declared on the root element so children don't
    # need explicit prefixes.
    ET.register_namespace("", ATOM_NS)
    feed = ET.Element(f"{{{ATOM_NS}}}feed")
    ET.SubElement(feed, f"{{{ATOM_NS}}}title").text = CHANNEL_TITLE
    ET.SubElement(feed, f"{{{ATOM_NS}}}subtitle").text = CHANNEL_DESCRIPTION
    ET.SubElement(
        feed,
        f"{{{ATOM_NS}}}link",
        attrib={"href": site_url, "rel": "alternate", "type": "text/html"},
    )
    ET.SubElement(
        feed,
        f"{{{ATOM_NS}}}link",
        attrib={
            "href": f"{site_url.rstrip('/')}/feed.atom",
            "rel": "self",
            "type": "application/atom+xml",
        },
    )
    ET.SubElement(feed, f"{{{ATOM_NS}}}id").text = (
        f"{site_url.rstrip('/')}/feed.atom"
    )
    ET.SubElement(feed, f"{{{ATOM_NS}}}updated").text = _iso8601(now)
    ET.SubElement(feed, f"{{{ATOM_NS}}}generator").text = GENERATOR

    author = ET.SubElement(feed, f"{{{ATOM_NS}}}author")
    ET.SubElement(author, f"{{{ATOM_NS}}}name").text = "altdata-brief"
    ET.SubElement(author, f"{{{ATOM_NS}}}uri").text = (
        "https://github.com/Leonard-Don/altdata-brief"
    )

    cdata_blocks: dict[str, str] = {}
    for item in items:
        entry = ET.SubElement(feed, f"{{{ATOM_NS}}}entry")
        ET.SubElement(entry, f"{{{ATOM_NS}}}title").text = item["title"]

        kind = item.get("kind", "brief")
        suffix = item.get("file_suffix", "")
        if kind == "digest":
            link_path = f"digests/{item['date']}{suffix}.html"
            guid_prefix = "altdata-brief:digest"
        elif kind == "monthly":
            link_path = f"digests/{item['date']}{suffix}.html"
            guid_prefix = "altdata-brief:monthly"
        else:
            link_path = f"briefs/{item['date']}{suffix}.html"
            guid_prefix = "altdata-brief"
        link_url = f"{site_url.rstrip('/')}/{link_path}"
        ET.SubElement(
            entry,
            f"{{{ATOM_NS}}}link",
            attrib={
                "href": link_url,
                "rel": "alternate",
                "type": "text/html",
            },
        )

        guid_lang = item.get("guid_lang") or ""
        guid_tail = f":{guid_lang}" if guid_lang else ""
        ET.SubElement(entry, f"{{{ATOM_NS}}}id").text = (
            f"urn:{guid_prefix}:{item['date']}{guid_tail}"
        )
        # Atom requires ISO-8601 — we already have RFC 822 for RSS;
        # synthesize a fresh ISO timestamp from the date.
        ET.SubElement(entry, f"{{{ATOM_NS}}}updated").text = _atom_date_from_brief_date(
            item["date"]
        )
        ET.SubElement(entry, f"{{{ATOM_NS}}}published").text = (
            _atom_date_from_brief_date(item["date"])
        )

        summary_el = ET.SubElement(
            entry, f"{{{ATOM_NS}}}summary", attrib={"type": "text"}
        )
        summary_el.text = item["description"]

        content_el = ET.SubElement(
            entry, f"{{{ATOM_NS}}}content", attrib={"type": "html"}
        )
        _set_cdata_text(
            content_el,
            f"<p>{_escape_feed_html_text(item['description'])}</p>",
            cdata_blocks,
        )

        for cat in item.get("categories") or ():
            ET.SubElement(
                entry, f"{{{ATOM_NS}}}category", attrib={"term": cat}
            )
        if kind == "digest":
            ET.SubElement(
                entry,
                f"{{{ATOM_NS}}}category",
                attrib={"term": "weekly-digest"},
            )
        elif kind == "monthly":
            ET.SubElement(
                entry,
                f"{{{ATOM_NS}}}category",
                attrib={"term": "monthly-digest"},
            )

        image_url = item.get("image_url")
        if image_url:
            ET.SubElement(
                entry,
                f"{{{ATOM_NS}}}link",
                attrib={
                    "rel": "enclosure",
                    "href": image_url,
                    "type": "image/png",
                    "length": str(item.get("image_length", 0)),
                },
            )

    _write_xml_with_cdata(feed_path, feed, cdata_blocks)
    return feed_path


def _set_cdata_text(
    element: ET.Element,
    body: str,
    cdata_blocks: dict[str, str],
) -> None:
    """Attach CDATA content using an unguessable post-serialization token."""
    token = f"CN_ALT_CDATA_{uuid4().hex}"
    element.text = token
    cdata_blocks[token] = body


def _write_xml_with_cdata(
    feed_path: Path,
    root: ET.Element,
    cdata_blocks: dict[str, str],
) -> None:
    ET.indent(root, space="  ", level=0)
    xml_text = ET.tostring(root, encoding="utf-8", xml_declaration=True).decode(
        "utf-8"
    )
    for token, body in cdata_blocks.items():
        xml_text = xml_text.replace(token, _cdata_section(body))
    feed_path.write_bytes(xml_text.encode("utf-8"))


def _cdata_section(body: str) -> str:
    return f"<![CDATA[{body.replace(']]>', ']]]]><![CDATA[>')}]]>"


def _escape_feed_html_text(text: str) -> str:
    escaped = html.escape(text or "", quote=False)
    return escaped.replace("]]&gt;", "]]>")


def _iso8601(dt: datetime) -> str:
    """Format ``dt`` as ISO-8601 with a trailing ``Z`` for UTC."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _atom_date_from_brief_date(date_str: str) -> str:
    """Return ISO-8601 09:00 UTC stamp for an Atom updated/published field.

    Accepts ``YYYY-MM-DD`` (daily), ``YYYY-Www`` (weekly digest), and
    ``YYYY-MM`` (v0.11 monthly digest) stems. For weekly digests we
    recover the Friday of that ISO week; for monthly digests we use
    the last day of the calendar month so the timestamp is monotonic
    across the merged feed.
    """
    if _looks_like_date(date_str):
        dt = datetime.strptime(date_str, "%Y-%m-%d").replace(hour=9, tzinfo=UTC)
        return _iso8601(dt)
    digest_re = re.compile(r"^(\d{4})-W(\d{2})$")
    m = digest_re.fullmatch(date_str)
    if m:
        friday = _iso_week_friday(int(m.group(1)), int(m.group(2)))
        dt = datetime.combine(friday, datetime.min.time()).replace(
            hour=18, tzinfo=UTC
        )
        return _iso8601(dt)
    monthly_re = re.compile(r"^(\d{4})-(\d{2})$")
    m = monthly_re.fullmatch(date_str)
    if m:
        year = int(m.group(1))
        month = int(m.group(2))
        if 1 <= month <= 12:
            if month == 12:
                last_day = (datetime(year + 1, 1, 1) - timedelta(days=1)).date()
            else:
                last_day = (datetime(year, month + 1, 1) - timedelta(days=1)).date()
            dt = datetime.combine(last_day, datetime.min.time()).replace(
                hour=17, tzinfo=UTC
            )
            return _iso8601(dt)
    # Last-resort fallback so we never emit an empty Atom timestamp
    # (would fail schema validation in strict clients).
    return _iso8601(datetime.now(UTC))
