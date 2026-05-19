"""政策动向 — top 3 policy_radar industries by |avg_impact|."""

from __future__ import annotations

from typing import Any

from cn_altdata_brief.adapters.base import AdapterPayload

SIGNAL_LABELS = {
    "bullish": "利好",
    "bearish": "利空",
    "neutral": "中性",
}


def synthesize_policy(payload: AdapterPayload | None) -> dict[str, Any]:
    """Return template context for the 政策动向 section.

    Output shape::

        {
            "available": bool,
            "title": "政策动向",
            "bullets": [str, ...],          # 3 bullets
            "top_industries": [...],         # raw rows for chart use
            "policy_count": int,
            "sources": [str],
        }
    """
    if payload is None:
        return _empty("super-pricing-system 数据缺失，未读取 policy_radar.json。")

    policy = payload.data.get("policy_radar", {}) or {}
    ranked = policy.get("industry_signals", []) or []
    if not ranked:
        return _empty(
            "policy_radar 当前样本为空，可能上游 ingest 失败。",
            sources=[payload.cache_label],
        )

    top = ranked[:3]
    bullets = [_format_bullet(row) for row in top]
    policy_path = payload.data.get("policy_cache_path")
    cache_label = (
        f"{payload.source}::policy_radar.json"
        if policy_path
        else payload.cache_label
    )
    return {
        "available": True,
        "title": "政策动向",
        "bullets": bullets,
        "top_industries": top,
        "policy_count": int(policy.get("policy_count", 0) or 0),
        "signal_score": policy.get("signal_score"),
        "confidence": policy.get("confidence"),
        "timestamp": policy.get("timestamp"),
        "sources": [cache_label],
    }


def _format_bullet(row: dict[str, Any]) -> str:
    industry = row.get("industry", "未知行业")
    impact = float(row.get("avg_impact", 0.0) or 0.0)
    mentions = int(row.get("mentions", 0) or 0)
    signal = SIGNAL_LABELS.get(str(row.get("signal", "neutral")), "中性")
    direction = "" if impact == 0 else ("正向" if impact > 0 else "负向")
    return (
        f"**{industry}**：政策影响={impact:+.3f}（{direction or '中性'}）· "
        f"提及次数={mentions} · 信号={signal}"
    )


def _empty(reason: str, *, sources: list[str] | None = None) -> dict[str, Any]:
    return {
        "available": False,
        "title": "政策动向",
        "bullets": [f"_数据缺失_：{reason}"],
        "top_industries": [],
        "policy_count": 0,
        "signal_score": None,
        "confidence": None,
        "timestamp": None,
        "sources": sources or [],
    }
