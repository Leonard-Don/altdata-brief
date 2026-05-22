"""Static site index — minimal jekyll-friendly markdown listing briefs and (v0.9) digests."""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

INDEX_HEADER = """# 多市场另类数据日报 — 历史归档

每个交易日 09:00 (UTC+8) 自动生成的研究简报。免费、可订阅、可引用。
v0.9 起每周五 18:00 还会发布一份本周回顾，聚合本周 5 份日报。

> 本项目通过 4 个公开摘要/快照数据源合成日报，详见 [README](../README.md)。

## 简报列表

"""

DIGEST_SECTION_HEADER = """
## 本周回顾

"""

_DIGEST_STEM_RE = re.compile(r"^\d{4}-W\d{2}$")


def render_site_index(
    briefs_dir: Path,
    output_path: Path | None = None,
    digests_dir: Path | None = None,
) -> Path:
    """Build index.md listing every brief in `briefs_dir` newest-first.

    v0.9 — also lists weekly digests from ``digests_dir`` (defaults to
    ``<briefs_dir parent>/digests``). When that directory does not
    exist the digest section is omitted entirely, preserving the
    pre-v0.9 single-section layout.

    Returns the path to the written index file.
    """
    briefs_dir = Path(briefs_dir)
    briefs_dir.mkdir(parents=True, exist_ok=True)
    target = output_path or (briefs_dir / "index.md")

    briefs = sorted(
        (
            p
            for p in briefs_dir.glob("*.md")
            if _looks_like_daily_brief_filename(p.stem)
        ),
        reverse=True,
    )
    lines = [INDEX_HEADER]
    if not briefs:
        lines.append("_暂无简报，请运行 `uv run altdata-brief generate` 生成首份。_\n")
    else:
        lines.append(
            f"共 {len(briefs)} 份日报，覆盖 {briefs[-1].stem} 至 {briefs[0].stem}；最新在前。\n\n"
        )
        for b in briefs:
            lines.append(f"- [{b.stem}]({b.name})\n")

    inferred_digests = digests_dir or (briefs_dir.parent / "digests")
    inferred_digests = Path(inferred_digests)
    if inferred_digests.exists():
        digests = sorted(
            (
                p
                for p in inferred_digests.glob("*.md")
                if _looks_like_digest_filename(p.stem)
            ),
            reverse=True,
        )
        lines.append(DIGEST_SECTION_HEADER)
        if not digests:
            lines.append(
                "_本周回顾尚未生成（每周五 18:00 由 launchd 自动产出）。_\n"
            )
        else:
            for d in digests:
                lines.append(f"- [{d.stem}](../digests/{d.name})\n")

    target.write_text("".join(lines), encoding="utf-8")
    return target


def _looks_like_digest_filename(stem: str) -> bool:
    """``2026-W20`` is a digest; ``2026-W20.en`` is its EN sibling."""
    base = stem.split(".", 1)[0]
    return bool(_DIGEST_STEM_RE.fullmatch(base))


def _looks_like_daily_brief_filename(stem: str) -> bool:
    """Return True only for canonical daily brief stems: ``YYYY-MM-DD``."""
    try:
        return date.fromisoformat(stem).isoformat() == stem
    except ValueError:
        return False
