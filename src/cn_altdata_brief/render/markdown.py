"""Jinja2-driven markdown rendering of the brief."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape

from cn_altdata_brief.timefmt import format_beijing_time

DEFAULT_TEMPLATE_DIR = Path(__file__).resolve().parents[3] / "templates"


def _raw_observation_text(observation: dict[str, Any] | None) -> str:
    if not observation:
        return ""
    raw_text = observation.get("raw_text")
    if isinstance(raw_text, str):
        return raw_text
    sentences = observation.get("sentences") or []
    return "\n".join(str(s) for s in sentences)


def _default_llm_context(observation: dict[str, Any] | None = None) -> dict[str, Any]:
    raw_text = _raw_observation_text(observation)
    return {
        "requested": False,
        "used": False,
        "status": "disabled",
        "model": None,
        "latency_ms": None,
        "input_tokens": None,
        "output_tokens": None,
        "raw_hash": hashlib.sha256(raw_text.encode("utf-8")).hexdigest()[:16]
        if raw_text
        else "",
        "note": None,
    }


def _env(template_dir: Path) -> Environment:
    env = Environment(
        loader=FileSystemLoader(str(template_dir)),
        autoescape=select_autoescape(default=False),
        trim_blocks=True,
        lstrip_blocks=True,
        undefined=StrictUndefined,
    )
    # Expose the Beijing-time formatter to every template under the
    # ``beijing_time`` name. Frontmatter timestamps stay raw (machine
    # readers parse them as ISO 8601 / RFC 3339); the formatter is
    # opt-in per template line so we only convert user-visible body
    # copy and leave the metadata blocks alone.
    env.globals["beijing_time"] = format_beijing_time
    return env


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
          "llm": {...},  # optional; defaults to disabled when omitted
        }
    """
    tdir = Path(template_dir) if template_dir else DEFAULT_TEMPLATE_DIR
    env = _env(tdir)
    template = env.get_template(template_name)
    render_context = dict(context)
    if "llm" not in render_context:
        observation = render_context.get("observation")
        render_context["llm"] = _default_llm_context(
            observation if isinstance(observation, dict) else None
        )
    return template.render(**render_context)
