#!/usr/bin/env python3
"""Regenerate the checked-in README sample charts and preview image.

The README images are static artifacts, so this script keeps them in sync with
the current chart renderer and avoids stale mixed-language screenshots.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import matplotlib  # noqa: E402

matplotlib.use("Agg")
import matplotlib.image as mpimg  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
from matplotlib.patches import FancyBboxPatch  # noqa: E402

from altdata_brief.render.charts import render_all_charts  # noqa: E402

OUT_DIR = ROOT / "docs" / "sample_charts"
FIXTURE_ROOT = ROOT / "tests" / "fixtures"
PREVIEW_PATH = OUT_DIR / "brief_preview.png"


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    render_all_charts(
        output_dir=OUT_DIR,
        policy_top=_policy_rows(),
        metals=_metal_rows(),
        industry_top=_industry_rows(),
        nav_trend=_nav_rows(),
    )
    _render_preview()


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _policy_rows() -> list[dict[str, Any]]:
    payload = _read_json(FIXTURE_ROOT / "public_summary" / "alt_data_summary.json")
    signals = payload["providers"]["policy_radar"]["industry_signals"]
    rows = [
        {
            "industry": industry,
            "avg_impact": float(info.get("avg_impact", 0.0) or 0.0),
            "mentions": int(info.get("mentions", 0) or 0),
            "signal": info.get("signal", "neutral"),
        }
        for industry, info in signals.items()
    ]
    rows.sort(key=lambda row: (abs(row["avg_impact"]), row["mentions"]), reverse=True)
    return rows[:3]


def _metal_rows() -> list[dict[str, Any]]:
    payload = _read_json(FIXTURE_ROOT / "public_summary" / "alt_data_summary.json")
    metals = payload["providers"]["macro_hf"]["metals"]
    names = {"aluminium": "铝", "copper": "铜", "nickel": "镍"}
    rows = [
        {
            "metal": metal,
            "name_cn": names.get(metal, metal),
            "price_change_pct": float(info.get("weekly_change_pct", 0.0) or 0.0),
        }
        for metal, info in metals.items()
    ]
    rows.sort(key=lambda row: abs(row["price_change_pct"]), reverse=True)
    return rows[:3]


def _industry_rows() -> list[dict[str, Any]]:
    payload = _read_json(FIXTURE_ROOT / "public_summary" / "quant_summary.json")
    return payload["providers"]["industry_heat"]["top_industries_by_score"][:3]


def _nav_rows() -> list[dict[str, Any]]:
    payload = _read_json(FIXTURE_ROOT / "etf_512400" / "liveSnapshot.json")
    return payload["navTrend"][-5:]


def _render_preview() -> None:
    fig = plt.figure(figsize=(14, 9.8), dpi=100)
    fig.patch.set_facecolor("#f6f8fa")

    _header(fig)
    _section_panel(fig)
    _chart_card(fig, [0.41, 0.50, 0.265, 0.31], "政策动向", OUT_DIR / "policy_impact.png")
    _chart_card(fig, [0.70, 0.50, 0.265, 0.31], "库存信号", OUT_DIR / "inventory_change.png")
    _chart_card(fig, [0.41, 0.13, 0.265, 0.31], "ETF 净值", OUT_DIR / "etf_nav.png")
    _chart_card(fig, [0.70, 0.13, 0.265, 0.31], "行业温度", OUT_DIR / "industry_heat.png")

    _rounded_box(fig, 0.03, 0.04, 0.94, 0.04, "#eef4f8", "#d0d7de", radius=0.014)
    fig.text(
        0.048,
        0.059,
        "每段都保留来源、缓存文件和时间戳，便于研究复核与审计。",
        fontsize=11,
        color="#334155",
        va="center",
    )

    fig.savefig(PREVIEW_PATH, bbox_inches="tight", pad_inches=0)
    plt.close(fig)


def _header(fig: plt.Figure) -> None:
    _rounded_box(fig, 0, 0.85, 1, 0.15, "#12445d", "#12445d", radius=0)
    fig.text(
        0.035,
        0.94,
        "AltData Brief — 2026-05-17",
        fontsize=30,
        color="white",
        va="center",
    )
    fig.text(
        0.036,
        0.895,
        "基于公开数据包的确定性多市场研究简报",
        fontsize=15,
        color="#e6edf3",
        va="center",
    )
    _rounded_box(fig, 0.765, 0.906, 0.18, 0.045, "#ddf4e8", "#2da44e", radius=0.018)
    fig.text(0.855, 0.928, "规则化 + 可审计", fontsize=12, color="#116329", ha="center", va="center")


def _section_panel(fig: plt.Figure) -> None:
    _rounded_box(fig, 0.03, 0.11, 0.35, 0.66, "#ffffff", "#d0d7de", radius=0.012)
    fig.text(0.052, 0.735, "日报结构", fontsize=17, color="#1f2937", va="center")

    items = [
        ("01 政策动向", "按政策影响和提及强度筛选重点行业。"),
        ("02 库存信号", "把金属价格和库存变化压缩成风险语言。"),
        ("03 ETF 资金流", "跟踪 ETF 行情、净值溢价和源健康度。"),
        ("04 行业温度", "汇总跨项目热度排行和政策叠加信号。"),
    ]
    y = 0.63
    for title, body in items:
        _rounded_box(fig, 0.05, y, 0.31, 0.115, "#f6fbff", "#d8e3ef", radius=0.010)
        fig.text(0.066, y + 0.080, title, fontsize=16, color="#075985", va="center")
        fig.text(0.066, y + 0.044, body, fontsize=11.5, color="#475569", va="center")
        y -= 0.145

    _rounded_box(fig, 0.05, 0.145, 0.31, 0.035, "#ddf4e8", "#b7dfc5", radius=0.012)
    fig.text(
        0.205,
        0.162,
        "输出：Markdown 简报 + 图表 + RSS 归档",
        fontsize=10.5,
        color="#116329",
        ha="center",
        va="center",
    )


def _chart_card(fig: plt.Figure, rect: list[float], title: str, path: Path) -> None:
    x, y, width, height = rect
    _rounded_box(fig, x, y, width, height, "#ffffff", "#d0d7de", radius=0.012)
    fig.text(x + 0.015, y + height - 0.035, title, fontsize=13.5, color="#334155", va="center")

    ax = fig.add_axes([x + 0.025, y + 0.095, width - 0.05, height - 0.16])
    ax.set_zorder(5)
    ax.patch.set_alpha(0)
    ax.imshow(_cropped_chart(path), aspect="auto")
    ax.axis("off")


def _cropped_chart(path: Path) -> np.ndarray:
    image = mpimg.imread(path)
    rgb = image[..., :3]
    non_white = np.any(rgb < 0.985, axis=2)
    rows, cols = np.where(non_white)
    if not len(rows) or not len(cols):
        return image

    pad = 20
    y0 = max(int(rows.min()) - pad, 0)
    y1 = min(int(rows.max()) + pad, image.shape[0] - 1)
    x0 = max(int(cols.min()) - pad, 0)
    x1 = min(int(cols.max()) + pad, image.shape[1] - 1)
    return image[y0 : y1 + 1, x0 : x1 + 1]


def _rounded_box(
    fig: plt.Figure,
    x: float,
    y: float,
    width: float,
    height: float,
    facecolor: str,
    edgecolor: str,
    *,
    radius: float,
) -> None:
    box = FancyBboxPatch(
        (x, y),
        width,
        height,
        boxstyle=f"round,pad=0,rounding_size={radius}",
        transform=fig.transFigure,
        facecolor=facecolor,
        edgecolor=edgecolor,
        linewidth=0.8,
        zorder=-1,
    )
    fig.patches.append(box)


if __name__ == "__main__":
    main()
