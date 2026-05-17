"""Static site index — minimal jekyll-friendly markdown listing all briefs."""

from __future__ import annotations

from pathlib import Path

INDEX_HEADER = """# CN AltData Brief — 历史归档

每个交易日 09:00 (UTC+8) 自动生成的研究简报。免费、可订阅、可引用。

> 本项目通过 6 个量化项目的真实信号合成日报，详见 [README](../README.md)。

## 简报列表

"""


def render_site_index(briefs_dir: Path, output_path: Path | None = None) -> Path:
    """Build index.md listing every brief in `briefs_dir` newest-first.

    Returns the path to the written index file.
    """
    briefs_dir = Path(briefs_dir)
    briefs_dir.mkdir(parents=True, exist_ok=True)
    target = output_path or (briefs_dir / "index.md")

    briefs = sorted(
        (p for p in briefs_dir.glob("*.md") if p.name != "index.md"),
        reverse=True,
    )
    lines = [INDEX_HEADER]
    if not briefs:
        lines.append("_暂无简报，请运行 `uv run cn-altdata-brief generate` 生成首份。_\n")
    else:
        for b in briefs:
            lines.append(f"- [{b.stem}]({b.name})\n")

    target.write_text("".join(lines), encoding="utf-8")
    return target
