"""v0.9 — render the weekly digest markdown via Jinja2.

Mirrors the shape of :mod:`cn_altdata_brief.render.markdown` so the
CLI surface is symmetric. We keep the digest template separate from
the daily brief template because the section layout, frontmatter, and
audience differ — readers consume a daily brief in 60 seconds and a
weekly digest in five minutes.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape

DEFAULT_TEMPLATE_DIR = Path(__file__).resolve().parents[3] / "templates"


def _env(template_dir: Path) -> Environment:
    return Environment(
        loader=FileSystemLoader(str(template_dir)),
        autoescape=select_autoescape(default=False),
        trim_blocks=True,
        lstrip_blocks=True,
        undefined=StrictUndefined,
    )


def render_weekly_digest_markdown(
    *,
    context: dict[str, Any],
    template_dir: Path | None = None,
    template_name: str = "weekly_digest.md.j2",
) -> str:
    """Render the weekly digest. ``context`` is the dict from
    :meth:`cn_altdata_brief.digest.WeeklyDigest.render_context`.
    """
    tdir = Path(template_dir) if template_dir else DEFAULT_TEMPLATE_DIR
    env = _env(tdir)
    template = env.get_template(template_name)
    return template.render(**context)
