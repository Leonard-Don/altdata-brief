"""ETF 资金流 — ETF 512400 health + adjacent ETF heat overlay."""

from __future__ import annotations

from typing import Any

from cn_altdata_brief.adapters.base import AdapterPayload


def synthesize_etf_flow(
    etf_payload: AdapterPayload | None,
    quant_payload: AdapterPayload | None,
) -> dict[str, Any]:
    """Combine ETF 512400 snapshot with quant-trading industry heat."""
    if etf_payload is None:
        return _empty("ETF 512400 liveSnapshot.json 缺失。")

    snap = etf_payload.data
    quote_line = _format_quote(snap)
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
            "邻近行业热度：" + " · ".join(f"{row['industry']} ({row['heat_score']:.2f})" for row in adjacent)
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
    price = snap.get("price")
    change_pct = snap.get("change_percent")
    amount = snap.get("amount_cny")
    turnover = snap.get("turnover_rate")
    parts = [f"**{name}** ({snap.get('code', '512400')})"]
    if price is not None:
        parts.append(f"现价 {price:.3f}")
    if change_pct is not None:
        parts.append(f"涨跌 {float(change_pct) * 100:+.2f}%")
    if amount:
        parts.append(f"成交额 {float(amount) / 1e8:.2f} 亿")
    if turnover is not None:
        parts.append(f"换手 {float(turnover) * 100:.2f}%")
    return " · ".join(parts)


def _format_nav(snap: dict[str, Any]) -> str:
    nav = snap.get("nav", {}) or {}
    date = nav.get("date", "—")
    unit = nav.get("unit")
    daily = nav.get("daily_return")
    parts = [f"净值（{date}）"]
    if unit is not None:
        parts.append(f"单位净值 {unit:.4f}")
    if daily is not None:
        parts.append(f"日收益 {float(daily) * 100:+.2f}%")
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
