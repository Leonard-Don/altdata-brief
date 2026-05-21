"""ETF 资金流 — ETF 512400 health + adjacent ETF heat overlay."""

from __future__ import annotations

import math
from typing import Any

from cn_altdata_brief.adapters.base import AdapterPayload
from cn_altdata_brief.timefmt import format_beijing_time


def synthesize_etf_flow(
    etf_payload: AdapterPayload | None,
    quant_payload: AdapterPayload | None,
) -> dict[str, Any]:
    """Combine ETF 512400 snapshot with quant-trading industry heat."""
    if etf_payload is None:
        return _empty("ETF 512400 liveSnapshot.json 缺失。")

    snap = etf_payload.data
    quote_line = (
        _format_quote(snap)
        if _quote_source_usable(snap)
        else _format_quote_unavailable(snap)
    )
    nav_line = _format_nav(snap)
    health = snap.get("source_health", {}) or {}
    health_line = (
        f"必需数据源 {health.get('required_ok', 0)}/{health.get('required_total', 0)} 正常 · "
        f"兜底次数={health.get('fallback', 0)} · 评级={health.get('verdict', '未知')}"
    )
    drivers = snap.get("commodity_drivers", {}) or {}
    driver_line = (
        f"商品驱动子源 {drivers.get('ok_count', 0)}/{drivers.get('total', 0)} 正常"
        if drivers.get("total")
        else None
    )

    bullets = [quote_line, nav_line, health_line]
    if driver_line:
        bullets.append(driver_line)

    adjacent = _adjacent_industries(quant_payload)
    if adjacent:
        bullets.append(
            "邻近行业热度：" + " · ".join(f"{row.get('industry', '未知')} ({float(row.get('heat_score', 0.0) or 0.0):.2f})" for row in adjacent)
        )

    sources = [etf_payload.cache_label]
    if quant_payload is not None:
        sources.append(quant_payload.cache_label)

    return {
        "available": True,
        "title": "ETF 资金流",
        "bullets": bullets,
        "quote": snap,
        "adjacent": adjacent,
        "sources": sources,
    }


def _format_quote(snap: dict[str, Any]) -> str:
    name = snap.get("name", "ETF 512400")
    price = _coerce_float(snap.get("price"))
    change_pct = _coerce_float(snap.get("change_percent"))
    amount = _coerce_float(snap.get("amount_cny"))
    turnover = _coerce_float(snap.get("turnover_rate"))
    parts = [f"**{name}** ({snap.get('code', '512400')})"]
    if price is not None:
        parts.append(f"现价 {price:.3f}")
    if change_pct is not None:
        parts.append(f"涨跌 {change_pct * 100:+.2f}%")
    if amount:
        parts.append(f"成交额 {amount / 1e8:.2f} 亿")
    if turnover is not None:
        parts.append(f"换手 {turnover * 100:.2f}%")
    return " · ".join(parts)


def _coerce_float(value: Any) -> float | None:
    """Return a finite float for numeric public-summary scalars, else ``None``."""
    if value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _quote_source_usable(snap: dict[str, Any]) -> bool:
    """Only publish current-price language when the quote source is healthy."""
    health = snap.get("source_health", {}) or {}
    if "quote_ok" in health or "quote_fallback" in health:
        return bool(health.get("quote_ok")) and not bool(health.get("quote_fallback"))

    sources = health.get("sources") or []
    if isinstance(sources, list):
        quote = next((s for s in sources if isinstance(s, dict) and s.get("id") == "quote"), None)
        if quote is not None:
            return bool(quote.get("ok")) and not bool(quote.get("fallback"))

    return health.get("verdict") != "降级"


def _format_quote_unavailable(snap: dict[str, Any]) -> str:
    trade_date = snap.get("trade_date") or "未知"
    raw_generated_at = snap.get("generated_at")
    # Convert the upstream snapshot's UTC ISO timestamp to Beijing time
    # for the brief body. When the value is missing or unparseable the
    # formatter returns the input untouched, preserving the "未知"
    # fallback path.
    generated_at = (
        format_beijing_time(raw_generated_at) if raw_generated_at else "未知"
    )
    return (
        f"**{snap.get('name', 'ETF 512400')}** ({snap.get('code', '512400')}) · "
        f"行情源降级，当前价未采用（快照交易日={trade_date}，生成时间={generated_at}）"
    )


def _format_nav(snap: dict[str, Any]) -> str:
    nav = snap.get("nav", {}) or {}
    date = nav.get("date", "—")
    unit = _coerce_float(nav.get("unit"))
    daily = _coerce_float(nav.get("daily_return"))
    parts = [f"净值（{date}）"]
    if unit is not None:
        parts.append(f"单位净值 {unit:.4f}")
    if daily is not None:
        parts.append(f"日收益 {daily * 100:+.2f}%")
    return " · ".join(parts)


def _adjacent_industries(payload: AdapterPayload | None) -> list[dict[str, Any]]:
    """Pick industries adjacent to 512400 (有色 / 新能源 / 工业金属)."""
    if payload is None:
        return []
    keywords = ("有色", "新能源", "金属", "电网", "光伏", "锂")
    rows = payload.data.get("industries", []) or []
    matched = [r for r in rows if any(k in str(r.get("industry", "")) for k in keywords)]
    return matched[:3]


def _empty(reason: str) -> dict[str, Any]:
    return {
        "available": False,
        "title": "ETF 资金流",
        "bullets": [f"_数据缺失_：{reason}"],
        "quote": {},
        "adjacent": [],
        "sources": [],
    }
