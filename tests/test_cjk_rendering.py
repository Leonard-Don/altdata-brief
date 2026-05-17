"""CI sanity test: matplotlib must render Chinese characters in a chart.

This guards against the v0.1 gap where charts were saved on the
GitHub Actions Ubuntu runner without a CJK font installed (the
default Noto fallback only ships in ``fonts-noto-cjk``, which the
runner lacks unless explicitly apt-installed). When the font is
missing, matplotlib substitutes glyphs with rectangles — the chart
still saves but every Chinese label becomes "tofu". This test fails
fast in that case.
"""

from __future__ import annotations

import warnings
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.font_manager as fm  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402

CJK_SAMPLE = "新能源汽车 · 库存信号"


def test_cjk_font_available_and_module_picked_one() -> None:
    """A CJK font is registered, and the charts module detected it."""
    from cn_altdata_brief.render import charts as charts_mod

    available = {f.name for f in fm.fontManager.ttflist}
    cjk_present = bool(set(charts_mod._CJK_CANDIDATES) & available)
    assert cjk_present, (
        "No CJK font found. Install fonts-noto-cjk (Ubuntu) or PingFang SC "
        f"(macOS). Searched: {charts_mod._CJK_CANDIDATES}"
    )
    assert charts_mod._cjk_choice in charts_mod._CJK_CANDIDATES
    assert charts_mod._cjk_choice in plt.rcParams["font.family"]


def test_matplotlib_renders_chinese_without_glyph_warning(tmp_path: Path) -> None:
    """Render a chart with Chinese title; fail on any glyph-missing warning."""
    out = tmp_path / "cjk_smoke.png"
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        fig, ax = plt.subplots(figsize=(4, 2.5))
        ax.bar(["铜", "铝", "镍"], [1.0, -0.5, 0.2])
        ax.set_title(CJK_SAMPLE)
        ax.set_xlabel("品种")
        ax.set_ylabel("周价格变化 (%)")
        fig.tight_layout()
        fig.savefig(out, dpi=120)
        plt.close(fig)

    glyph_warnings = [w for w in caught if "missing" in str(w.message).lower()]
    assert not glyph_warnings, (
        "matplotlib raised glyph-missing warnings — CJK font likely not "
        f"installed. Warnings:\n{[str(w.message) for w in glyph_warnings]}"
    )
    assert out.exists() and out.stat().st_size > 1500
