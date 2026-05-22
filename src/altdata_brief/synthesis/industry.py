"""行业温度 — top 3 industries from quant-trading heat with policy overlay."""

from __future__ import annotations

from typing import Any

from altdata_brief.adapters.base import AdapterPayload

SIGNAL_LABELS = {
    "bullish": "利好",
    "bearish": "利空",
    "neutral": "中性",
}


def synthesize_industry(payload: AdapterPayload | None) -> dict[str, Any]:
    """Return template context for the 行业温度 section."""
    if payload is None:
        return _empty("quant-trading-system 数据缺失。")

    rows = payload.data.get("industries", []) or []
    if not rows:
        return _empty(
            "行业热度样本为空。",
            sources=[payload.cache_label],
        )

    top = rows[:3]
    bullets = [_format_row(row) for row in top]
    return {
        "available": True,
        "title": "行业温度",
        "bullets": bullets,
        "top_industries": top,
        "sources": [payload.cache_label],
    }


def _format_row(row: dict[str, Any]) -> str:
    industry = row.get("industry", "未知")
    heat = float(row.get("heat_score", 0.0) or 0.0)
    signal = SIGNAL_LABELS.get(str(row.get("policy_signal", "neutral")), "中性")
    impact = float(row.get("policy_impact", 0.0) or 0.0)
    mentions = int(row.get("mentions", 0) or 0)
    # When both the policy impact and the mention count are zero there
    # is no policy signal worth surfacing — the upstream simply has
    # nothing attached to this industry yet. Rendering
    # ``中性（影响=+0.000）· 提及次数=0`` adds noise without any
    # information, so we collapse those rows to a single
    # ``无政策叠加`` marker that's quick to skim past.
    if impact == 0.0 and mentions == 0:
        return f"**{industry}**：热度={heat:.3f} · 无政策叠加"
    return (
        f"**{industry}**：热度={heat:.3f} · 政策叠加={signal}"
        f"（影响={impact:+.3f}）· 提及次数={mentions}"
    )


def _empty(reason: str, *, sources: list[str] | None = None) -> dict[str, Any]:
    return {
        "available": False,
        "title": "行业温度",
        "bullets": [f"_数据缺失_：{reason}"],
        "top_industries": [],
        "sources": sources or [],
    }
