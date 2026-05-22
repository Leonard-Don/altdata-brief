"""Matplotlib chart rendering — same restrained palette as forest plots."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")  # noqa: E402  - must be set before pyplot import
import matplotlib.font_manager as fm  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402

# Best-effort CJK fallback so 中文 labels render on macOS / Linux / GitHub Actions.
_CJK_CANDIDATES = (
    "PingFang SC",
    "Heiti SC",
    "Hiragino Sans GB",
    "Arial Unicode MS",
    # Ubuntu's fonts-noto-cjk package commonly registers the TTC as JP even
    # though it includes the shared CJK glyph coverage we need for Simplified
    # Chinese labels in CI.
    "Noto Sans CJK SC",
    "Noto Sans CJK JP",
    "Noto Sans CJK TC",
    "Noto Sans CJK HK",
    "Noto Sans CJK KR",
    "WenQuanYi Zen Hei",
    "SimHei",
    "Microsoft YaHei",
)
_available_fonts = {f.name for f in fm.fontManager.ttflist}
_cjk_choice = next((c for c in _CJK_CANDIDATES if c in _available_fonts), None)
if _cjk_choice is not None:
    plt.rcParams["font.family"] = [_cjk_choice, "DejaVu Sans"]
plt.rcParams["axes.unicode_minus"] = False

PALETTE = {
    "primary": "#1f6feb",
    "accent": "#6f42c1",
    "neutral": "#6e7681",
    "bullish": "#2da44e",
    "bearish": "#cf222e",
    "background": "#ffffff",
}


def render_all_charts(
    *,
    output_dir: Path,
    policy_top: list[dict[str, Any]] | None,
    metals: list[dict[str, Any]] | None,
    industry_top: list[dict[str, Any]] | None,
    nav_trend: list[dict[str, Any]] | None,
) -> dict[str, Path]:
    """Render up to 4 charts. Returns a name -> path mapping (only existing)."""
    output_dir.mkdir(parents=True, exist_ok=True)
    paths: dict[str, Path] = {}

    if policy_top:
        paths["policy"] = _bar_chart(
            output_dir / "policy_impact.png",
            labels=[r.get("industry", "未知") for r in policy_top],
            values=[float(r.get("avg_impact", 0.0) or 0.0) for r in policy_top],
            title="政策动向 · 前三行业政策影响",
            xlabel="政策影响均值",
        )

    if metals:
        paths["inventory"] = _bar_chart(
            output_dir / "inventory_change.png",
            labels=[m.get("name_cn") or m.get("metal", "?") for m in metals],
            values=[float(m.get("price_change_pct", 0.0) or 0.0) for m in metals],
            title="库存信号 · 金属周价格变化",
            xlabel="周价格变化 (%)",
        )

    if industry_top:
        paths["industry"] = _bar_chart(
            output_dir / "industry_heat.png",
            labels=[r.get("industry", "未知") for r in industry_top],
            values=[float(r.get("heat_score", 0.0) or 0.0) for r in industry_top],
            title="行业温度 · 前三行业热度",
            xlabel="热度分",
            single_color=PALETTE["accent"],
        )

    if nav_trend and len(nav_trend) >= 2:
        paths["nav"] = _line_chart(
            output_dir / "etf_nav.png",
            xs=[r.get("date", "") for r in nav_trend],
            ys=[float(r.get("unit", 0.0) or 0.0) for r in nav_trend],
            title="ETF 512400 · 近 5 个交易日单位净值",
        )

    return paths


# ---- chart primitives ------------------------------------------------


def _bar_chart(
    path: Path,
    *,
    labels: list[str],
    values: list[float],
    title: str,
    xlabel: str,
    single_color: str | None = None,
) -> Path:
    fig, ax = plt.subplots(figsize=(7.5, 0.7 + 0.5 * max(len(labels), 1)))
    fig.patch.set_facecolor(PALETTE["background"])
    if single_color:
        colors = [single_color] * len(values)
    else:
        colors = [PALETTE["bullish"] if v >= 0 else PALETTE["bearish"] for v in values]
    ax.barh(labels, values, color=colors, edgecolor=PALETTE["neutral"])
    ax.axvline(0, color=PALETTE["neutral"], linewidth=0.7)
    ax.set_title(title, fontsize=11)
    ax.set_xlabel(xlabel, fontsize=9)
    ax.tick_params(axis="both", labelsize=9)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    fig.tight_layout()
    fig.savefig(path, dpi=140)
    plt.close(fig)
    return path


def _line_chart(
    path: Path,
    *,
    xs: list[str],
    ys: list[float],
    title: str,
) -> Path:
    fig, ax = plt.subplots(figsize=(7.5, 3.2))
    fig.patch.set_facecolor(PALETTE["background"])
    ax.plot(xs, ys, marker="o", color=PALETTE["primary"], linewidth=1.6)
    ax.set_title(title, fontsize=11)
    ax.tick_params(axis="both", labelsize=8)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    fig.autofmt_xdate(rotation=30, ha="right")
    fig.tight_layout()
    fig.savefig(path, dpi=140)
    plt.close(fig)
    return path
