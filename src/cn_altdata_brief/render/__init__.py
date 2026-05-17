"""Rendering layer — markdown templates + matplotlib charts + static site."""

from cn_altdata_brief.render.charts import render_all_charts
from cn_altdata_brief.render.markdown import render_brief_markdown
from cn_altdata_brief.render.site import render_site_index

__all__ = [
    "render_all_charts",
    "render_brief_markdown",
    "render_site_index",
]
