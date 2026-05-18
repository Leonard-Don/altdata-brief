"""v0.9 — render the weekly digest markdown via Jinja2.

v0.11 adds a sibling :func:`render_monthly_digest_markdown` for the
monthly cadence. Both share the same Jinja environment factory; the
templates live next to each other so a designer can edit the weekly
and monthly views in one place.
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


def render_monthly_digest_markdown(
    *,
    context: dict[str, Any],
    template_dir: Path | None = None,
    template_name: str = "monthly_digest.md.j2",
) -> str:
    """Render the monthly digest. ``context`` is the dict from
    :meth:`cn_altdata_brief.digest.MonthlyDigest.render_context`.

    v0.11 — the monthly template uses the same Jinja env as weekly so
    StrictUndefined catches any missing key during testing. Empty
    sustained-themes / reversal-events lists render as a single
    placeholder bullet, mirroring the weekly degradation contract.
    """
    tdir = Path(template_dir) if template_dir else DEFAULT_TEMPLATE_DIR
    env = _env(tdir)
    template = env.get_template(template_name)
    return template.render(**context)
