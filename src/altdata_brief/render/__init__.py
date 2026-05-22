"""Rendering layer — markdown templates + matplotlib charts + static site."""

from altdata_brief.render.charts import render_all_charts
from altdata_brief.render.digest import (
    render_monthly_digest_markdown,
    render_weekly_digest_markdown,
)
from altdata_brief.render.markdown import render_brief_markdown
from altdata_brief.render.og_image import chart_url_for, pick_og_chart
from altdata_brief.render.rss import render_atom_feed, render_feed
from altdata_brief.render.site import render_site_index

__all__ = [
    "chart_url_for",
    "pick_og_chart",
    "render_all_charts",
    "render_atom_feed",
    "render_brief_markdown",
    "render_feed",
    "render_monthly_digest_markdown",
    "render_site_index",
    "render_weekly_digest_markdown",
]
