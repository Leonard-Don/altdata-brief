"""库存信号 — LME + SHFE per-metal weekly change."""

from __future__ import annotations

from typing import Any

from cn_altdata_brief.adapters.base import AdapterPayload


def synthesize_inventory(payload: AdapterPayload | None) -> dict[str, Any]:
    """Return template context for the 库存信号 section."""
    if payload is None:
        return _empty("super-pricing-system 数据缺失，未读取 macro_hf.json。")

    macro = payload.data.get("macro_hf", {}) or {}
    metals = macro.get("metals", []) or []
    if not metals:
        return _empty(
            "macro_hf 库存样本为空，可能 LME/SHFE proxy 未连。",
            sources=[payload.cache_label],
        )

    bullets = [_format_metal(m) for m in metals]
    ports = macro.get("ports") or {}
    port_line = (
        f"全球港口拥堵指数 {ports.get('global_index')}（{ports.get('status', '未知')}）· "
        f"跟踪港口数={ports.get('tracked_ports')}"
        if ports
        else None
    )
    macro_path = payload.data.get("macro_cache_path")
    cache_label = (
        f"{payload.source}::macro_hf.json"
        if macro_path
        else payload.cache_label
    )
    return {
        "available": True,
        "title": "库存信号",
        "metals": metals,
        "bullets": bullets,
        "ports": ports,
        "port_line": port_line,
        "timestamp": macro.get("timestamp"),
        "sources": [cache_label],
    }


def _format_metal(m: dict[str, Any]) -> str:
    name = m.get("name_cn") or m.get("metal", "未知")
    change = float(m.get("price_change_pct", 0.0) or 0.0)
    trend = _trend_label(str(m.get("trend", "stable")))
    vol = float(m.get("volatility", 0.0) or 0.0)
    conf = float(m.get("confidence", 0.0) or 0.0)
    direction_tag = _tag_for(change, trend)
    # When the public-summary upstream has no weekly-change data,
    # _normalize_macro_from_public hardcodes both change and volatility
    # to 0.0. Treat that as "no data" (same pattern as M2 industry fix
    # in commit 1e92acd) instead of rendering '周价格变化 +0.00% · 波动率 0.0'
    # which reads like real but zero data.
    if change == 0.0 and vol == 0.0:
        prefix = "无周变动"
    else:
        prefix = f"周价格变化 {change:+.2f}% · 波动率 {vol:.1f}"
    return (
        f"**{name}**：{prefix} · "
        f"趋势={trend} · 标签={direction_tag} · 置信度={conf:.2f}"
    )


def _tag_for(change: float, trend: str) -> str:
    """Boring rule: |change| ≤ 0.5% → 持稳；change < 0 → 去库; change > 0 → 累库."""
    if trend in {"下行", "下降", "去库"}:
        return "去库"
    if trend in {"上行", "上升", "累库"}:
        return "累库"
    if abs(change) <= 0.5:
        return "持稳"
    return "去库" if change < 0 else "累库"


def _trend_label(trend: str) -> str:
    return {
        "falling": "下行",
        "down": "下行",
        "rising": "上行",
        "up": "上行",
        "restocking": "累库",
        "destocking": "去库",
        "stable": "稳定",
    }.get(trend, trend or "未知")


def _empty(reason: str, *, sources: list[str] | None = None) -> dict[str, Any]:
    return {
        "available": False,
        "title": "库存信号",
        "metals": [],
        "bullets": [f"_数据缺失_：{reason}"],
        "ports": None,
        "port_line": None,
        "timestamp": None,
        "sources": sources or [],
    }
