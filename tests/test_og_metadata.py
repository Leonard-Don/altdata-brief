"""v0.10 — tests for OpenGraph metadata + RSS/Atom enrichment.

Covers:

1. ``generate_og_tags`` returns every required OG / Twitter Card field.
2. ``pick_og_chart`` picks the chart with the highest absolute signal.
3. ``signal_categories_for`` filters out trivial signals.
4. ``render_meta_tags`` emits well-formed ``<meta>`` HTML.
5. RSS feed gains ``<enclosure>``, ``<category>``, CDATA description.
6. Atom 1.0 feed has the right schema (xmlns + entry/id/updated).
7. Brief HTML layout file contains the OG meta blocks.
8. Frontmatter injection preserves prior keys and rewrites og_* keys.
"""

from __future__ import annotations

import json
from pathlib import Path
from xml.etree import ElementTree as ET

from altdata_brief.publish.og_metadata import (
    DEFAULT_SITE_URL,
    _strip_md_emphasis,
    generate_og_tags,
    render_meta_tags,
    signal_categories_for,
)
from altdata_brief.render.og_image import (
    chart_url_for,
    pick_og_chart,
)
from altdata_brief.render.rss import render_atom_feed, render_feed

SAMPLE_BRIEF = """---
date: 2026-05-17
generated_at: 2026-05-17T13:58:17Z
---

# AltData Brief — 2026-05-17

> 由 `altdata-brief` 在 2026-05-17 自动生成。

---

## 1. 政策动向

- **新能源汽车**：avg_impact=-0.388 (负向) · mentions=94 · 信号=利空
- **电网**：avg_impact=+0.100 · mentions=8

## 2. 库存信号

- **铝**：周价格变化 -1.15% · 波动率 0.0
"""

SAMPLE_SECTIONS = {
    "policy": {
        "top_industries": [
            {"industry": "新能源汽车", "avg_impact": -0.388, "mentions": 94},
            {"industry": "电网", "avg_impact": 0.100, "mentions": 8},
        ]
    },
    "inventory": {
        "metals": [
            {"metal": "Al", "name_cn": "铝", "price_change_pct": -1.15},
            {"metal": "Cu", "name_cn": "铜", "price_change_pct": -0.68},
        ]
    },
    "industry": {"top_industries": []},
}


# ---------------------------------------------------------------------------
# 1. OG tag generation
# ---------------------------------------------------------------------------


def test_generate_og_tags_emits_required_fields(tmp_path: Path) -> None:
    brief = tmp_path / "2026-05-17.md"
    brief.write_text(SAMPLE_BRIEF, encoding="utf-8")
    chart_dir = tmp_path / "charts"
    chart_dir.mkdir()
    (chart_dir / "policy_impact.png").write_bytes(b"\x89PNG\r\n\x1a\n")

    tags = generate_og_tags(
        brief,
        site_url="https://example.com/altdata-brief",
        sections=SAMPLE_SECTIONS,
        chart_dir=chart_dir,
    )

    required_og = {
        "og:title",
        "og:description",
        "og:type",
        "og:url",
        "og:image",
        "og:locale",
        "og:site_name",
    }
    required_twitter = {
        "twitter:card",
        "twitter:site",
        "twitter:title",
        "twitter:description",
        "twitter:image",
    }
    for key in required_og | required_twitter:
        assert tags[key], f"missing OG/Twitter tag: {key}"

    assert "2026-05-17" in tags["og:title"]
    assert "新能源汽车" in tags["og:title"]
    assert tags["og:type"] == "article"
    assert tags["og:locale"] == "zh_CN"
    assert tags["og:site_name"] == "多市场另类数据日报"
    assert tags["twitter:card"] == "summary_large_image"
    assert tags["og:url"].endswith("/briefs/2026-05-17.html")
    assert tags["og:image"].startswith("https://example.com/altdata-brief/charts/")


def test_generate_og_tags_falls_back_when_brief_degraded(tmp_path: Path) -> None:
    brief = tmp_path / "2026-05-17.md"
    brief.write_text(
        "# AltData Brief — 2026-05-17\n\n## 1. 政策动向\n\n_数据缺失_\n",
        encoding="utf-8",
    )
    tags = generate_og_tags(brief, sections={}, chart_dir=None)
    # Title still works without a top industry.
    assert "2026-05-17" in tags["og:title"]
    # Description still non-empty (uses sample fallback).
    assert tags["og:description"]
    assert "og:image" not in tags
    assert "twitter:image" not in tags


# ---------------------------------------------------------------------------
# 2. Chart picker
# ---------------------------------------------------------------------------


def test_pick_og_chart_picks_strongest_signal(tmp_path: Path) -> None:
    chart_dir = tmp_path / "charts"
    chart_dir.mkdir()
    for name in ("policy_impact.png", "inventory_change.png", "industry_heat.png", "etf_nav.png"):
        (chart_dir / name).write_bytes(b"\x89PNG\r\n\x1a\n")  # min PNG magic

    # Policy avg_impact=-0.5 (scaled ×100 = 50). Inventory pct=-1.15. So
    # policy wins by 50 vs 1.15.
    sections = {
        "policy": {"top_industries": [{"industry": "X", "avg_impact": -0.5}]},
        "inventory": {"metals": [{"metal": "Al", "price_change_pct": -1.15}]},
        "industry": {"top_industries": [{"industry": "Y", "heat_score": 2.0}]},
    }
    key, path = pick_og_chart(sections, chart_dir)
    assert key == "policy"
    assert path is not None
    assert path.name == "policy_impact.png"


def test_pick_og_chart_inventory_when_policy_weak(tmp_path: Path) -> None:
    chart_dir = tmp_path / "charts"
    chart_dir.mkdir()
    for name in ("inventory_change.png", "etf_nav.png"):
        (chart_dir / name).write_bytes(b"\x89PNG")
    sections = {
        "policy": {"top_industries": [{"industry": "X", "avg_impact": 0.001}]},
        "inventory": {"metals": [{"metal": "Al", "price_change_pct": 12.5}]},
        "industry": {"top_industries": []},
    }
    key, path = pick_og_chart(sections, chart_dir)
    # Policy ×100 = 0.1, Inventory = 12.5 → inventory wins.
    assert key == "inventory"
    assert path is not None and path.name == "inventory_change.png"


def test_pick_og_chart_returns_none_when_chart_dir_missing() -> None:
    key, path = pick_og_chart(SAMPLE_SECTIONS, None)
    assert key is None
    assert path is None


def test_chart_url_for_uses_site_root() -> None:
    url = chart_url_for("policy", "2026-05-17", site_url="https://x.io/cn/")
    assert url == "https://x.io/cn/charts/2026-05-17/policy_impact.png"


# ---------------------------------------------------------------------------
# 3. Signal categories
# ---------------------------------------------------------------------------


def test_signal_categories_filters_trivial() -> None:
    cats = signal_categories_for(SAMPLE_SECTIONS)
    # 新能源汽车 has |0.388|>=0.05; 电网 has |0.1|>=0.05; 铝 has |1.15|>=0.1; 铜 has |0.68|>=0.1
    assert "新能源汽车" in cats
    assert "电网" in cats
    assert "铝" in cats
    assert len(cats) <= 5


def test_signal_categories_empty_for_no_signal() -> None:
    weak = {
        "policy": {"top_industries": [{"industry": "X", "avg_impact": 0.001}]},
        "inventory": {"metals": [{"metal": "Cu", "price_change_pct": 0.01}]},
    }
    assert signal_categories_for(weak) == []
    assert signal_categories_for(None) == []


# ---------------------------------------------------------------------------
# 4. Meta tag HTML emitter
# ---------------------------------------------------------------------------


def test_render_meta_tags_uses_correct_attr_per_namespace() -> None:
    tags = {
        "og:title": "Hello & 世界",
        "twitter:card": "summary_large_image",
        "article:section": "新能源",
    }
    html = render_meta_tags(tags)
    # OG / article use property=
    assert '<meta property="og:title"' in html
    assert '<meta property="article:section"' in html
    # Twitter uses name=
    assert '<meta name="twitter:card"' in html
    # Content HTML-escaped.
    assert "&amp;" in html


# ---------------------------------------------------------------------------
# 5. RSS enclosure / category / CDATA
# ---------------------------------------------------------------------------


def test_rss_feed_emits_enclosure_and_categories(tmp_path: Path) -> None:
    briefs = tmp_path / "briefs"
    briefs.mkdir()
    (briefs / "2026-05-17.md").write_text(SAMPLE_BRIEF, encoding="utf-8")
    chart_root = tmp_path / "charts"
    chart_root.mkdir()
    date_chart_dir = chart_root / "2026-05-17"
    date_chart_dir.mkdir()
    (date_chart_dir / "policy_impact.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 200)

    feed_path = tmp_path / "feed.xml"
    render_feed(
        briefs_dir=briefs,
        feed_path=feed_path,
        site_url="https://example.com/cn",
        chart_dir=chart_root,
        sections_by_date={"2026-05-17": SAMPLE_SECTIONS},
    )

    raw = feed_path.read_text(encoding="utf-8")
    # CDATA wrapping
    assert "<![CDATA[" in raw
    assert "]]>" in raw

    root = ET.parse(feed_path).getroot()
    item = root.find("channel/item")
    assert item is not None
    # Enclosure for OG image
    enclosure = item.find("enclosure")
    assert enclosure is not None
    assert enclosure.attrib["url"].endswith("policy_impact.png")
    assert enclosure.attrib["type"] == "image/png"
    assert int(enclosure.attrib["length"]) > 0
    # Categories
    cats = [c.text for c in item.findall("category")]
    assert "新能源汽车" in cats


def test_feed_escapes_adversarial_description_cdata_and_html(tmp_path: Path) -> None:
    briefs = tmp_path / "briefs"
    briefs.mkdir()
    # Sentinel tokens use single letters around the underscored payload
    # so the markdown ``__bold__`` stripper (now applied to feed text
    # to avoid leaking ``**bold**`` markers to readers) does not eat
    # them — we still want to verify CDATA escaping integrity here.
    adversarial = """# AltData Brief — 2026-05-17

- **注入测试**：safe ]]> xCDATA_OPENx xCDATA_CLOSEx <script>alert(1)</script> & <b>raw</b>
"""
    (briefs / "2026-05-17.md").write_text(adversarial, encoding="utf-8")

    rss_path = tmp_path / "feed.xml"
    render_feed(
        briefs_dir=briefs,
        feed_path=rss_path,
        site_url="https://example.com/cn",
    )
    rss_raw = rss_path.read_text(encoding="utf-8")
    assert "]]]]><![CDATA[>" in rss_raw
    assert "<script>" not in rss_raw
    rss_root = ET.parse(rss_path).getroot()
    rss_desc = rss_root.findtext("channel/item/description") or ""
    assert "]]>" in rss_desc
    assert "xCDATA_OPENx" in rss_desc
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in rss_desc
    assert "<script>" not in rss_desc

    atom_path = tmp_path / "feed.atom"
    render_atom_feed(
        briefs_dir=briefs,
        feed_path=atom_path,
        site_url="https://example.com/cn",
    )
    atom_raw = atom_path.read_text(encoding="utf-8")
    assert "]]]]><![CDATA[>" in atom_raw
    assert "<script>" not in atom_raw
    atom_root = ET.parse(atom_path).getroot()
    ns = "{http://www.w3.org/2005/Atom}"
    atom_content = atom_root.findtext(f"{ns}entry/{ns}content") or ""
    assert atom_content.startswith("<p>")
    assert "]]>" in atom_content
    assert "xCDATA_CLOSEx" in atom_content
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in atom_content
    assert "<script>" not in atom_content


def test_missing_chart_omits_og_and_feed_images_without_local_leakage(tmp_path: Path) -> None:
    briefs = tmp_path / "briefs"
    briefs.mkdir()
    brief = briefs / "2026-05-17.md"
    brief.write_text(SAMPLE_BRIEF, encoding="utf-8")
    chart_root = tmp_path / "SECRET_TOKEN_local_charts"
    date_chart_dir = chart_root / "2026-05-17"
    date_chart_dir.mkdir(parents=True)

    tags = generate_og_tags(
        brief,
        site_url="https://example.com/cn",
        sections=SAMPLE_SECTIONS,
        chart_dir=date_chart_dir,
    )
    assert "og:image" not in tags
    assert "twitter:image" not in tags
    assert "SECRET_TOKEN" not in "\n".join(tags.values())
    assert str(chart_root) not in "\n".join(tags.values())

    rss_path = tmp_path / "feed.xml"
    render_feed(
        briefs_dir=briefs,
        feed_path=rss_path,
        site_url="https://example.com/cn",
        chart_dir=chart_root,
        sections_by_date={"2026-05-17": SAMPLE_SECTIONS},
    )
    rss_raw = rss_path.read_text(encoding="utf-8")
    ET.parse(rss_path)
    assert "<enclosure" not in rss_raw
    assert "SECRET_TOKEN" not in rss_raw
    assert str(chart_root) not in rss_raw

    atom_path = tmp_path / "feed.atom"
    render_atom_feed(
        briefs_dir=briefs,
        feed_path=atom_path,
        site_url="https://example.com/cn",
        chart_dir=chart_root,
        sections_by_date={"2026-05-17": SAMPLE_SECTIONS},
    )
    atom_raw = atom_path.read_text(encoding="utf-8")
    ET.parse(atom_path)
    assert 'rel="enclosure"' not in atom_raw
    assert "SECRET_TOKEN" not in atom_raw
    assert str(chart_root) not in atom_raw


# ---------------------------------------------------------------------------
# 6. Atom 1.0 feed schema
# ---------------------------------------------------------------------------


def test_atom_feed_schema(tmp_path: Path) -> None:
    briefs = tmp_path / "briefs"
    briefs.mkdir()
    (briefs / "2026-05-17.md").write_text(SAMPLE_BRIEF, encoding="utf-8")
    (briefs / "2026-05-16.md").write_text(
        SAMPLE_BRIEF.replace("2026-05-17", "2026-05-16"), encoding="utf-8"
    )

    feed_path = tmp_path / "feed.atom"
    render_atom_feed(
        briefs_dir=briefs,
        feed_path=feed_path,
        site_url="https://example.com/cn",
        sections_by_date={
            "2026-05-17": SAMPLE_SECTIONS,
            "2026-05-16": SAMPLE_SECTIONS,
        },
    )

    raw = feed_path.read_text(encoding="utf-8")
    assert 'xmlns="http://www.w3.org/2005/Atom"' in raw

    # Parse with namespace.
    root = ET.parse(feed_path).getroot()
    ns = "{http://www.w3.org/2005/Atom}"
    assert root.tag == f"{ns}feed"
    assert root.find(f"{ns}id") is not None
    assert root.find(f"{ns}updated") is not None
    assert root.find(f"{ns}title").text == "多市场另类数据日报"
    entries = root.findall(f"{ns}entry")
    assert len(entries) == 2
    for e in entries:
        assert e.find(f"{ns}id") is not None
        assert e.find(f"{ns}title") is not None
        assert e.find(f"{ns}updated") is not None
        assert e.find(f"{ns}published") is not None
        assert e.find(f"{ns}summary") is not None


# ---------------------------------------------------------------------------
# 7. Brief HTML layout has OG meta inline
# ---------------------------------------------------------------------------


def test_brief_html_template_includes_og_meta() -> None:
    layout = Path(__file__).resolve().parents[1] / "gh-pages-template" / "_layouts" / "brief.html"
    text = layout.read_text(encoding="utf-8")
    for needle in (
        'property="og:title"',
        'property="og:description"',
        'property="og:image"',
        'property="og:url"',
        'name="twitter:card"',
        'name="twitter:image"',
        "summary_large_image",
        'rel="alternate" type="application/atom+xml"',
    ):
        assert needle in text, f"layout missing {needle}"
    for needle in (
        "{{ brief_title | escape }}",
        "{{ brief_description | escape }}",
        "{{ brief_url | escape }}",
        "{{ og_image_url | escape }}",
        "{{ share_title | escape }}",
        "{{ share_url | escape }}",
    ):
        assert needle in text, f"layout missing Liquid escape filter for {needle}"
    assert "charts/default.png" not in text


# ---------------------------------------------------------------------------
# 8. CLI _inject_og_frontmatter helper
# ---------------------------------------------------------------------------


def test_inject_og_frontmatter_preserves_existing_keys(tmp_path: Path) -> None:
    from altdata_brief.cli import _inject_og_frontmatter

    brief = tmp_path / "2026-05-17.md"
    brief.write_text(SAMPLE_BRIEF, encoding="utf-8")

    tags = generate_og_tags(
        brief,
        site_url=DEFAULT_SITE_URL,
        sections=SAMPLE_SECTIONS,
        chart_dir=None,
    )
    _inject_og_frontmatter(brief, tags)

    out = brief.read_text(encoding="utf-8")
    # OG keys are present.
    assert "og_title:" in out
    assert "og_image:" in out
    assert "layout:" in out
    # Pre-existing key (date) is preserved.
    assert "date: 2026-05-17" in out

    # Re-run is idempotent (no duplicate og_title).
    _inject_og_frontmatter(brief, tags)
    out2 = brief.read_text(encoding="utf-8")
    assert out2.count("og_title:") == 1


# ---------------------------------------------------------------------------
# 9. Markdown emphasis stripper for OG / Twitter fields
# ---------------------------------------------------------------------------


def test_strip_md_emphasis_bold_double_asterisks() -> None:
    assert _strip_md_emphasis("**word**") == "word"


def test_strip_md_emphasis_real_brief_og_line() -> None:
    raw = "**中文**：x · y"
    assert _strip_md_emphasis(raw) == "中文：x · y"


def test_strip_md_emphasis_nested_strong() -> None:
    # ``***x***`` is bold+italic in Markdown; both layers should collapse.
    assert _strip_md_emphasis("***strong***") == "strong"


def test_strip_md_emphasis_leaves_math_asterisks_alone() -> None:
    # Asterisks surrounded by whitespace are arithmetic, not emphasis.
    assert _strip_md_emphasis("2 * 3 = 6") == "2 * 3 = 6"


def test_strip_md_emphasis_unchanged_when_no_emphasis() -> None:
    plain = "纯文本无样式 plain prose"
    assert _strip_md_emphasis(plain) == plain


def test_strip_md_emphasis_idempotent() -> None:
    raw = "**AI算力**：政策影响=+1.000（正向）· `code` __also__ *italic*"
    once = _strip_md_emphasis(raw)
    twice = _strip_md_emphasis(once)
    assert once == twice


def test_generate_og_tags_strips_emphasis_from_user_facing_fields(tmp_path: Path) -> None:
    brief = tmp_path / "2026-05-19.md"
    brief.write_text(
        """---
date: 2026-05-19
---

# AltData Brief — 2026-05-19

## 1. 政策动向

- **AI算力**：政策影响=+1.000（正向）· 提及次数=146 · 信号=利好
""",
        encoding="utf-8",
    )

    tags = generate_og_tags(brief, sections={}, chart_dir=None)

    # User-facing prose fields lose the ``**`` markers...
    assert "**" not in tags["og:title"]
    assert "**" not in tags["og:description"]
    assert "**" not in tags["twitter:title"]
    assert "**" not in tags["twitter:description"]
    assert "**" not in tags["article:section"]
    # ...but the content survives.
    assert "AI算力" in tags["og:description"]
    # URLs / machine fields are untouched.
    assert tags["og:url"].endswith("/briefs/2026-05-19.html")
    assert tags["og:type"] == "article"
    assert tags["og:locale"] == "zh_CN"


def test_inject_og_frontmatter_serializes_adversarial_values(tmp_path: Path) -> None:
    from altdata_brief.cli import _inject_og_frontmatter

    brief = tmp_path / "2026-05-17.md"
    brief.write_text(SAMPLE_BRIEF, encoding="utf-8")
    evil_title = 'quote" slash\\ newline\nlayout: evil\n---\n<script>bad</script>\rcontrol:\x01'
    evil_description = "desc with ]]> and backslash \\ and \nnew line"
    tags = {
        "og:title": evil_title,
        "og:description": evil_description,
        "og:url": 'https://example.com/?q="><img src=x onerror=alert(1)>',
        "og:image": "https://example.com/charts/2026-05-17/policy_impact.png",
        "og:locale": "zh_CN",
        "article:section": "Alt\n---\nData",
        "twitter:site": "@altdata_brief",
        "article:published_time": "2026-05-17T09:00:00Z",
    }

    _inject_og_frontmatter(brief, tags)
    lines = brief.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "---"
    end_idx = next(i for i, line in enumerate(lines[1:], start=1) if line == "---")
    frontmatter = lines[1:end_idx]
    serialized = dict(line.split(": ", 1) for line in frontmatter if ": " in line)

    assert json.loads(serialized["og_title"]) == evil_title
    assert json.loads(serialized["og_description"]) == evil_description
    assert json.loads(serialized["og_section"]) == "Alt\n---\nData"
    assert json.loads(serialized["layout"]) == "brief"
    assert "layout: evil" not in frontmatter
    assert "\x01" not in "\n".join(frontmatter)
    assert all(line != "---" for line in frontmatter)
