"""行业温度 — top 3 industries from quant-trading heat with policy overlay."""

from __future__ import annotations

from typing import Any

from cn_altdata_brief.adapters.base import AdapterPayload


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
    signal = row.get("policy_signal", "neutral")
    impact = float(row.get("policy_impact", 0.0) or 0.0)
    mentions = int(row.get("mentions", 0) or 0)
    return (
        f"**{industry}**：heat={heat:.3f} · 政策叠加 signal={signal} "
        f"(impact={impact:+.3f}) · mentions={mentions}"
    )


def _empty(reason: str, *, sources: list[str] | None = None) -> dict[str, Any]:
    return {
        "available": False,
        "title": "行业温度",
        "bullets": [f"_数据缺失_：{reason}"],
        "top_industries": [],
        "sources": sources or [],
    }
