"""v0.10 — OpenGraph / Twitter Card metadata for shared briefs.

When a brief URL is pasted into Twitter, WeChat, Substack, Slack, or any
modern feed reader, the consumer scrapes ``<meta>`` tags out of the
HTML ``<head>`` to render a preview card. v0.9 left the briefs as bare
markdown — the preview was just a URL stub. v0.10 generates the full
set of OpenGraph + Twitter Card tags so every shared brief turns into a
proper artifact with title, description, image, locale.

This module is **pure metadata generation**. The Jekyll layout
(``gh-pages-template/_layouts/brief.html``) consumes the same data shape
at render time — see :func:`render_meta_tags` for the HTML emitter that
the publisher embeds inline into the head of each per-brief page.

Why we do this at publish time rather than in the layout: GitHub Pages
runs Jekyll without Liquid filters that would let us cheaply look up
"the strongest-signal chart for this date". Doing it in Python keeps
the logic deterministic and unit-testable, and the layout just inlines
the resulting HTML block as a single Liquid include.
"""

from __future__ import annotations

import html
import re
from pathlib import Path
from typing import Any

from cn_altdata_brief.render.og_image import chart_url_for, pick_og_chart

SITE_NAME = "CN AltData Brief"
DEFAULT_SITE_URL = "https://leonard-don.github.io/cn-altdata-brief"
DEFAULT_TWITTER_HANDLE = "@cn_altdata"  # placeholder; user can override

# Pull the first bullet-point industry name from the policy section.
_TOP_POLICY_RE = re.compile(
    r"^- \*\*(?P<name>[^*]+)\*\*[^\n]*",
    flags=re.MULTILINE,
)

# Skip prefixes for description selection — same logic as RSS but kept
# in lockstep so the description here matches what shows in the feed.
_DESCRIPTION_SKIP_PREFIXES = (
    "#",
    ">",
    "---",
    "![",
    "**Sources:**",
    "{%",
    "<!--",
)

# Twitter Card description has a hard 200-char limit per spec; OG is
# loose but most consumers truncate around 300. We pick 200 to satisfy
# both.
_DESCRIPTION_MAX_CHARS = 200


def generate_og_tags(
    brief_path: Path,
    *,
    site_url: str = DEFAULT_SITE_URL,
    twitter_handle: str = DEFAULT_TWITTER_HANDLE,
    sections: dict[str, Any] | None = None,
    chart_dir: Path | None = None,
    locale: str = "zh_CN",
) -> dict[str, str]:
    """Return a flat ``{tag_name: content}`` dict for one brief.

    ``brief_path`` is the markdown source — its filename ``YYYY-MM-DD.md``
    determines the date and public URL. ``sections`` (the synthesized
    dict from the generator) and ``chart_dir`` feed the chart picker;
    when omitted we fall back to "highest-priority chart by name" so
    the function stays useful in standalone tests.

    The returned dict keys are the literal ``property`` / ``name``
    attribute values (e.g. ``og:title``, ``twitter:card``) — easier to
    interpolate into HTML than nested objects.
    """
    date = brief_path.stem
    if "." in date:
        # Skip language siblings (``2026-05-17.en.md`` → ``2026-05-17``).
        date = date.split(".", 1)[0]

    text = brief_path.read_text(encoding="utf-8") if brief_path.exists() else ""

    top_industry = _extract_top_policy_industry(text)
    description = _extract_description(text)
    chart_key, chart_path = pick_og_chart(sections, chart_dir)
    image_url = (
        chart_url_for(chart_key, date, site_url=site_url)
        if chart_key and chart_path is not None
        else ""
    )

    title = (
        f"{date} · {top_industry}"
        if top_industry
        else f"{date} · CN AltData Brief"
    )
    brief_url = f"{site_url.rstrip('/')}/briefs/{date}.html"

    tags = {
        # OpenGraph (Facebook / WeChat / Substack / LinkedIn etc.)
        "og:title": title,
        "og:description": description,
        "og:type": "article",
        "og:url": brief_url,
        "og:locale": locale,
        "og:site_name": SITE_NAME,
        # Twitter Cards — summary_large_image gives a full-width preview
        # which works best for the chart pack's landscape PNGs.
        "twitter:card": "summary_large_image",
        "twitter:site": twitter_handle,
        "twitter:title": title,
        "twitter:description": description,
        # Article-specific (consumed by Substack-style readers).
        "article:published_time": f"{date}T09:00:00Z",
        "article:section": top_industry or "Alt Data",
    }
    if image_url:
        tags["og:image"] = image_url
        tags["twitter:image"] = image_url
    return tags


def render_meta_tags(tags: dict[str, str]) -> str:
    """Render a ``{tag: content}`` dict as ``<meta>`` lines.

    Output uses ``property=`` for ``og:``/``article:`` tags and ``name=``
    for ``twitter:`` (per spec — Twitter explicitly disallows
    ``property``). Content is HTML-escaped.
    """
    lines: list[str] = []
    for tag, content in tags.items():
        if not content:
            continue
        escaped = html.escape(content or "", quote=True)
        if tag.startswith("twitter:"):
            attr = "name"
        else:
            attr = "property"
        lines.append(f'<meta {attr}="{tag}" content="{escaped}">')
    return "\n".join(lines)


def signal_categories_for(sections: dict[str, Any] | None) -> list[str]:
    """Return RSS ``<category>`` tags worth attaching to a brief.

    Used by the RSS / Atom enrichment. We surface up to 5 categories
    that say something semantic about the day's signals — industries
    with non-trivial policy impact, metals showing inventory moves.
    """
    if not sections:
        return []
    cats: list[str] = []
    for r in (sections.get("policy") or {}).get("top_industries") or []:
        name = str(r.get("industry") or "").strip()
        impact = abs(float(r.get("avg_impact", 0.0) or 0.0))
        if name and impact >= 0.05:
            cats.append(name)
    for m in (sections.get("inventory") or {}).get("metals") or []:
        name = str(m.get("name_cn") or m.get("metal") or "").strip()
        pct = abs(float(m.get("price_change_pct", 0.0) or 0.0))
        if name and pct >= 0.1:
            cats.append(name)
    # De-dupe while preserving order.
    seen: set[str] = set()
    deduped: list[str] = []
    for c in cats:
        if c in seen:
            continue
        seen.add(c)
        deduped.append(c)
    return deduped[:5]


# ---------------------------------------------------------------------------


def _extract_top_policy_industry(brief_md: str) -> str | None:
    """Return the first policy industry name, or None when brief is degraded."""
    # Limit to the policy section to avoid matching e.g. industry-heat names.
    policy_idx = brief_md.find("## 1. 政策动向")
    if policy_idx < 0:
        return None
    next_section_idx = brief_md.find("\n## ", policy_idx + 1)
    end = next_section_idx if next_section_idx > 0 else len(brief_md)
    policy_chunk = brief_md[policy_idx:end]
    match = _TOP_POLICY_RE.search(policy_chunk)
    if not match:
        return None
    return match.group("name").strip()


def _extract_description(brief_md: str) -> str:
    """Return the brief's first non-meta sentence (max ~200 chars).

    Skips any leading YAML frontmatter block (``---\\n…\\n---``) so the
    description isn't accidentally pulled from frontmatter keys when
    the OG-injected brief is re-read for description extraction
    (e.g. after running ``generate`` twice).
    """
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
        if line.startswith(_DESCRIPTION_SKIP_PREFIXES):
            continue
        if line.startswith("- "):
            text = line.lstrip("- ").strip()
        else:
            text = line
        return _truncate(text, _DESCRIPTION_MAX_CHARS)
    return (
        "Daily research brief synthesizing alt-data signals from 4 public "
        "source adapters."
    )


def _truncate(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    # Truncate at a word boundary if we can, else hard-cut.
    cut = text[: max_chars - 1].rsplit(" ", 1)[0]
    if not cut:
        cut = text[: max_chars - 1]
    return cut + "…"
