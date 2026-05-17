"""Jinja2-driven markdown rendering of the brief."""

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


def render_brief_markdown(
    *,
    context: dict[str, Any],
    template_dir: Path | None = None,
    template_name: str = "brief.md.j2",
) -> str:
    """Render the full brief markdown.

    ``context`` shape (keys MUST be present; values can carry `available=False`)::

        {
          "date": "2026-05-17",
          "policy": {...},
          "inventory": {...},
          "etf_flow": {...},
          "industry": {...},
          "observation": {...},
          "charts": {"policy": "charts/.../policy.png", ...} | {},
          "fetched_at": "2026-05-17T01:23:45Z",
        }
    """
    tdir = Path(template_dir) if template_dir else DEFAULT_TEMPLATE_DIR
    env = _env(tdir)
    template = env.get_template(template_name)
    return template.render(**context)
