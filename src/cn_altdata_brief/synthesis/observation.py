"""本日观察 — 3-sentence cross-cutting takeaway, deterministic templates.

v0.2 rewrites the v0.1 rule-based observation into a journalistic
*framing → contextualize → follow-up* sequence:

* sentence 1 — "今日核心信号是 X" (the headline)
* sentence 2 — "对比近 7 日，X …" (context vs. mock baseline)
* sentence 3 — "若该信号延续 N 日，可重点观察 X 板块/品种" (research follow-up)

The wording is still deterministic (no LLM). Baselines come from
:mod:`cn_altdata_brief.synthesis.baseline` constants — v0.3 will swap
those for the real super-pricing narrative archive.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from cn_altdata_brief.adapters.base import AdapterPayload
from cn_altdata_brief.synthesis.baseline import (
    BASELINE_ETF_NAV_VOL_7D,
    BASELINE_INDUSTRY_HEAT_7D,
    BASELINE_METAL_SPREAD_7D,
    BASELINE_POLICY_IMPACT_7D,
    SIGNAL_PERSISTENCE_DAYS,
    describe_intensity,
    load_recent_history,
)


@dataclass(slots=True)
class _Candidate:
    """One ranked candidate for the headline signal."""

    strength: float  # absolute magnitude — higher wins
    framing: str
    context: str
    action: str


_BOLDED_RE = re.compile(r"\*\*([^*]+)\*\*")
_NON_INDUSTRY_BOLD_TOKENS = {"良好", "一般", "较弱", "未知", "OK", "WARN", "FAIL"}
_SIGNAL_LABELS = {
    "bullish": "利好",
    "bearish": "利空",
    "neutral": "中性",
}


def _industries_from_candidate(cand: _Candidate) -> list[str]:
    """Pull bolded industry / commodity names from a candidate's sentences.

    The deterministic builders always emit ``**新能源汽车**`` style markup
    around the name(s) the v0.7 LLM rephrase layer must preserve. Order is
    framing → context → action; duplicates are de-duped while keeping the
    first occurrence's position.
    """
    seen: dict[str, None] = {}
    for chunk in (cand.framing, cand.context, cand.action):
        for match in _BOLDED_RE.findall(chunk):
            # Sometimes a candidate bolds a compound like "铜/铝" — split
            # on the slash so the validator can match each side.
            for piece in match.split("/"):
                piece = piece.strip()
                if piece and piece not in _NON_INDUSTRY_BOLD_TOKENS and piece not in seen:
                    seen[piece] = None
    return list(seen)


def synthesize_observation(
    super_payload: AdapterPayload | None,
    quant_payload: AdapterPayload | None,
    index_payload: AdapterPayload | None,
    etf_payload: AdapterPayload | None,
) -> dict[str, Any]:
    """Cross-section observation: rule-based, NOT an LLM call.

    Picks the strongest candidate across all available payloads and
    emits exactly 3 sentences in the framing/context/action shape.
    Degrades gracefully — missing source = fewer candidates, never an
    exception.
    """
    history = load_recent_history()  # v0.3 hook; today returns None
    _ = history  # constants below already encode v0.2 baselines

    candidates: list[_Candidate] = []
    sources: list[str] = []

    if super_payload is not None:
        sources.append(super_payload.cache_label)
        cand = _policy_candidate(super_payload)
        if cand:
            candidates.append(cand)
        cand = _inventory_candidate(super_payload)
        if cand:
            candidates.append(cand)

    if etf_payload is not None:
        sources.append(etf_payload.cache_label)
        cand = _etf_candidate(etf_payload)
        if cand:
            candidates.append(cand)

    if quant_payload is not None:
        sources.append(quant_payload.cache_label)
        cand = _industry_candidate(quant_payload)
        if cand:
            candidates.append(cand)

    if index_payload is not None:
        sources.append(index_payload.cache_label)
        cand = _index_candidate(index_payload)
        if cand:
            candidates.append(cand)

    if not candidates:
        sentences = ["_数据缺失_：所有上游均未返回有效信号，今日无可生成观察。"]
        return {
            "available": False,
            "title": "本日观察",
            "sentences": sentences,
            "raw_text": "\n".join(sentences),
            "industries": [],
            "sources": sources,
        }

    candidates.sort(key=lambda c: c.strength, reverse=True)
    lead = candidates[0]
    sentences = [lead.framing, lead.context, lead.action]
    return {
        "available": True,
        "title": "本日观察",
        "sentences": sentences,
        # v0.7: raw_text is the canonical deterministic prose used by the
        # optional LLM rephrase layer. Joined with newlines so paraphrase
        # boundaries remain visible to the model.
        "raw_text": "\n".join(sentences),
        # v0.7: keep the bolded industry/品种 names handy so the LLM
        # validator can verify they survived the rewrite without re-parsing.
        "industries": _industries_from_candidate(lead),
        "sources": sources,
    }


# -- per-source candidate builders -------------------------------------


def _policy_candidate(payload: AdapterPayload) -> _Candidate | None:
    policy = payload.data.get("policy_radar", {}) or {}
    ranked = policy.get("industry_signals", []) or []
    if not ranked:
        return None
    top = ranked[0]
    impact = float(top.get("avg_impact", 0.0) or 0.0)
    if abs(impact) < 0.05:
        return None
    direction = "偏空" if impact < 0 else "偏多"
    industry = top.get("industry", "未命名行业")
    mentions = int(top.get("mentions", 0) or 0)

    framing = (
        f"今日核心信号是 **{industry}** 的政策口径{direction}收敛"
        f"（政策影响={impact:+.3f}，提及次数={mentions}）。"
    )
    intensity = describe_intensity(abs(impact), BASELINE_POLICY_IMPACT_7D)
    context = (
        f"对比近 7 日政策影响基线≈{BASELINE_POLICY_IMPACT_7D:.2f}，"
        f"该信号强度{intensity}，政策雷达当批次累计 {policy.get('policy_count', 0)} 条记录。"
    )
    action = (
        f"若该信号延续 {SIGNAL_PERSISTENCE_DAYS} 日，可重点观察 **{industry}** 板块"
        "的资金流与情绪扩散是否同向。"
    )
    return _Candidate(
        strength=abs(impact) + 0.001,  # tiny boost so policy ties beat 0-strength
        framing=framing,
        context=context,
        action=action,
    )


def _inventory_candidate(payload: AdapterPayload) -> _Candidate | None:
    macro = payload.data.get("macro_hf", {}) or {}
    metals = macro.get("metals", []) or []
    if len(metals) < 2:
        return None
    changes = [(float(m.get("price_change_pct", 0.0) or 0.0), m) for m in metals]
    leader = max(changes, key=lambda kv: kv[0])
    laggard = min(changes, key=lambda kv: kv[0])
    spread = leader[0] - laggard[0]
    if spread < 0.3:
        return None

    framing = (
        f"今日核心信号是金属内部分化：**{leader[1].get('name_cn')}** "
        f"({leader[0]:+.2f}%) 与 **{laggard[1].get('name_cn')}** "
        f"({laggard[0]:+.2f}%) 周价差 {spread:.2f}%。"
    )
    intensity = describe_intensity(spread, BASELINE_METAL_SPREAD_7D)
    context = (
        f"对比近 7 日基线均值 {BASELINE_METAL_SPREAD_7D:.2f}%，"
        f"今日分化{intensity}，提示上下游需求节奏正在切换。"
    )
    action = (
        f"若该信号延续 {SIGNAL_PERSISTENCE_DAYS} 日，可重点观察 "
        f"**{leader[1].get('name_cn')}/{laggard[1].get('name_cn')}** 库存分化复核窗口，"
        "结合港口拥堵指数二次确认。"
    )
    return _Candidate(
        strength=spread,
        framing=framing,
        context=context,
        action=action,
    )


def _etf_candidate(payload: AdapterPayload) -> _Candidate | None:
    nav = payload.data.get("nav", {}) or {}
    daily = nav.get("daily_return")
    if daily is None:
        return None
    daily = float(daily)
    if abs(daily) < 0.005:
        return None
    pct = daily * 100
    direction = "上涨" if daily > 0 else "下跌"
    health = (payload.data.get("source_health") or {}).get("verdict", "未知")

    framing = (
        f"今日核心信号是 ETF 512400 日内 NAV {direction} {abs(pct):.2f}%，"
        f"数据源评级 **{health}**。"
    )
    intensity = describe_intensity(abs(daily), BASELINE_ETF_NAV_VOL_7D)
    context = (
        f"对比近 7 日波动均值 ≈{BASELINE_ETF_NAV_VOL_7D * 100:.2f}%，"
        f"今日波幅{intensity}；商品驱动子源 "
        f"{(payload.data.get('commodity_drivers') or {}).get('ok_count', 0)}"
        f"/{(payload.data.get('commodity_drivers') or {}).get('total', 0)} 正常。"
    )
    action = (
        f"若该信号延续 {SIGNAL_PERSISTENCE_DAYS} 日，可重点观察 **有色金属** 现货成交，"
        "收盘后复核折溢价、申赎与数据源一致性。"
    )
    return _Candidate(
        strength=abs(daily) * 100,  # convert to % so it competes with policy impact
        framing=framing,
        context=context,
        action=action,
    )


def _industry_candidate(payload: AdapterPayload) -> _Candidate | None:
    rows = payload.data.get("industries", []) or []
    if not rows:
        return None
    top = rows[0]
    heat = float(top.get("heat_score", 0.0) or 0.0)
    if heat < 0.1:
        return None

    name = top.get("industry", "未知")
    framing = (
        f"今日核心信号是行业热度榜首 **{name}**"
        f"（热度={heat:.3f}，政策口径={_SIGNAL_LABELS.get(str(top.get('policy_signal')), '中性')}）。"
    )
    intensity = describe_intensity(heat, BASELINE_INDUSTRY_HEAT_7D)
    context = (
        f"对比近 7 日热度均值 ≈{BASELINE_INDUSTRY_HEAT_7D:.2f}，"
        f"今日热度{intensity}，关注政策叠加是否同向放大。"
    )
    action = (
        f"若该信号延续 {SIGNAL_PERSISTENCE_DAYS} 日，可重点观察 **{name}** "
        "及其上下游 ETF 的资金外溢方向。"
    )
    return _Candidate(
        strength=heat,
        framing=framing,
        context=context,
        action=action,
    )


def _index_candidate(payload: AdapterPayload) -> _Candidate | None:
    pap = payload.data.get("pap_changes", []) or []
    if pap:
        framing = (
            f"今日核心信号是指数纳入研究的 PAP 比对发现 {len(pap)} 条假说裁决迁移。"
        )
        context = (
            "对比近 7 日通常 0–1 条迁移，今日研究框架出现新一致性事件，"
            "建议复核对应 H 编号的证据更新。"
        )
        action = (
            f"若该信号延续 {SIGNAL_PERSISTENCE_DAYS} 日，可重点观察是否需要"
            "上调或下调相应假说的证据层级。"
        )
        return _Candidate(
            strength=0.5 + len(pap) * 0.1,  # PAP migrations are rare → mid-high prior
            framing=framing,
            context=context,
            action=action,
        )
    verdicts = payload.data.get("verdicts") or []
    if not verdicts:
        return None
    supported = sum(1 for v in verdicts if v.get("verdict") == "支持")
    framing = (
        f"今日核心信号是指数纳入研究中 {len(verdicts)} 条 CMA 裁决保持稳定，"
        f"{supported}/{len(verdicts)} 为「支持」。"
    )
    context = (
        "对比近 7 日 PAP 偏离记录无迁移事件，研究框架在新数据下未触发翻盘信号。"
    )
    action = (
        f"若该信号延续 {SIGNAL_PERSISTENCE_DAYS} 日，可重点观察现有「支持」假说"
        "的证据引用是否进入复测窗口。"
    )
    return _Candidate(
        strength=0.05,  # baseline; gets out-prioritized when any policy/etf signal fires
        framing=framing,
        context=context,
        action=action,
    )
