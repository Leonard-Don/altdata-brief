"""v0.10 — pick the most signal-rich chart from a brief's chart pack.

When a brief is shared on Twitter / WeChat / Substack the OpenGraph
``og:image`` meta tag dictates the preview thumbnail. Each brief
generates up to 4 charts (``policy_impact``, ``inventory_change``,
``industry_heat``, ``etf_nav``) and we want to feature the one with the
strongest signal so the preview is informative, not generic.

The picker is intentionally pure-Python (no extra deps) and operates on
the **synthesized sections dict** (the same data that fed the chart
renderer) so the decision is reproducible from disk without re-opening
the PNG files.

Selection priority (highest signal wins):

1. ``policy`` — max absolute ``avg_impact`` across top industries
2. ``inventory`` — max absolute ``price_change_pct`` across metals
3. ``industry`` — max ``heat_score``
4. ``nav`` — fallback when nothing else has a signal

Ties break in the priority order above. The chart pack file naming is
fixed (the v0.6 chart renderer always writes
``policy_impact.png`` / ``inventory_change.png`` / ``industry_heat.png``
/ ``etf_nav.png``), so we hard-code the mapping.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

# Chart pack filenames — kept in sync with render.charts.render_all_charts.
_CHART_FILENAME = {
    "policy": "policy_impact.png",
    "inventory": "inventory_change.png",
    "industry": "industry_heat.png",
    "nav": "etf_nav.png",
}

# Default fallback when low-signal sections still produced a chart.
_FALLBACK_KEY = "nav"


def pick_og_chart(
    sections: dict[str, Any] | None,
    chart_dir: Path | None,
) -> tuple[str | None, Path | None]:
    """Return ``(chart_key, chart_path)`` for the strongest-signal chart.

    Parameters
    ----------
    sections:
        Synthesized brief sections. Expected shape — same as
        ``cli._cmd_generate`` builds:
        ``{"policy": {"top_industries": [...]}, "inventory":
        {"metals": [...]}, "industry": {"top_industries": [...]}}``.
        ``None`` or partial input is fine; we degrade gracefully.
    chart_dir:
        The directory holding the chart PNGs (e.g.
        ``output/charts/2026-05-17``). ``None`` means "no charts on
        disk" — we return ``(None, None)`` so callers do not emit a
        preview URL that would 404.

    Returns
    -------
    ``(key, path)`` where ``key`` is one of ``policy`` / ``inventory``
    / ``industry`` / ``nav`` and ``path`` points at an existing PNG.
    If no chart file exists, both values are ``None``.
    """
    scores = _compute_signal_scores(sections or {})

    # Sort by (score, priority) — priority breaks ties between charts
    # that all have score 0 (e.g. brief with degraded inputs).
    priority_order = {"policy": 0, "inventory": 1, "industry": 2, "nav": 3}
    ranked = sorted(
        scores.items(),
        key=lambda kv: (-kv[1], priority_order.get(kv[0], 99)),
    )

    if chart_dir is None or not chart_dir.exists():
        return None, None

    # Walk the ranking and pick the first one whose PNG actually exists.
    for key, _score in ranked:
        candidate = chart_dir / _CHART_FILENAME[key]
        if candidate.exists():
            return key, candidate

    # Nothing on disk matched. Try the fallback explicitly, and if even
    # that's missing return (None, None) so callers suppress image tags.
    fallback_path = chart_dir / _CHART_FILENAME[_FALLBACK_KEY]
    if fallback_path.exists():
        return _FALLBACK_KEY, fallback_path
    return None, None


def _compute_signal_scores(sections: dict[str, Any]) -> dict[str, float]:
    """Score each chart by its strongest absolute signal value.

    Higher = more signal. All scores live on a comparable scale so we
    can rank across heterogeneous chart types — the magnitude is what
    matters for "would this make a striking preview".
    """
    policy_top = (sections.get("policy") or {}).get("top_industries") or []
    metals = (sections.get("inventory") or {}).get("metals") or []
    industry_top = (sections.get("industry") or {}).get("top_industries") or []

    # Policy: avg_impact ranges [-1, +1]. We multiply by 100 so it
    # competes on the same scale as percentage-based signals.
    policy_signal = max(
        (abs(float(r.get("avg_impact", 0.0) or 0.0)) for r in policy_top),
        default=0.0,
    ) * 100.0

    inventory_signal = max(
        (abs(float(m.get("price_change_pct", 0.0) or 0.0)) for m in metals),
        default=0.0,
    )

    industry_signal = max(
        (float(r.get("heat_score", 0.0) or 0.0) for r in industry_top),
        default=0.0,
    )

    # NAV chart has no scalar signal — it's the time series itself. We
    # give it a tiny baseline so it wins the tiebreak when everything
    # else is zero (covers the "data missing" path).
    nav_signal = 0.01

    return {
        "policy": policy_signal,
        "inventory": inventory_signal,
        "industry": industry_signal,
        "nav": nav_signal,
    }


def chart_url_for(
    chart_key: str,
    date: str,
    *,
    site_url: str,
) -> str:
    """Build the public URL the OG meta tag should point at.

    ``site_url`` is the gh-pages site root (e.g.
    ``https://leonard-don.github.io/cn-altdata-brief``). Charts always
    live at ``charts/<date>/<filename>.png`` thanks to the publisher.
    """
    filename = _CHART_FILENAME.get(chart_key, _CHART_FILENAME[_FALLBACK_KEY])
    return f"{site_url.rstrip('/')}/charts/{date}/{filename}"
