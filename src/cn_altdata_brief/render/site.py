"""Static site index — minimal jekyll-friendly markdown listing briefs and (v0.9) digests."""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

try:
    # Prefer the tz database via zoneinfo — picks up DST rules and stays
    # correct across future changes. China observes a fixed +08:00 with
    # no DST, but we keep the same code path as the rest of the
    # codebase so a future migration (e.g. to ``Asia/Hong_Kong``) is
    # one rename.
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    try:
        _BEIJING_TZ: timezone = ZoneInfo("Asia/Shanghai")  # type: ignore[assignment]
    except ZoneInfoNotFoundError:  # pragma: no cover - tzdata missing
        _BEIJING_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")
except ImportError:  # pragma: no cover - Python < 3.9 fallback
    _BEIJING_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")


def _format_beijing_time(
    dt_or_str: datetime | str,
    *,
    with_seconds: bool = False,
    with_label: bool = True,
) -> str:
    """Render a timestamp in Beijing time for the Chinese brief body.

    Accepts a ``datetime`` or an ISO 8601 string (with or without
    trailing ``Z``). Naive datetimes / naive strings are assumed to be
    UTC — this matches every adapter and CLI emitter, all of which
    produce UTC timestamps. The function never raises on malformed
    input: when parsing fails the original string is returned so the
    brief still ships something legible.

    Parameters
    ----------
    with_seconds:
        Include ``HH:MM:SS`` instead of ``HH:MM``. Default ``False`` —
        most reader-facing copy is happier with minute precision.
    with_label:
        Append the ``" 北京时间"`` suffix. Default ``True``. Set to
        ``False`` only when the surrounding text already says "北京
        时间" (e.g. inside a sentence that opens with it).
    """
    if isinstance(dt_or_str, str):
        raw = dt_or_str.strip()
        if not raw:
            return raw
        # ``datetime.fromisoformat`` accepts a trailing ``Z`` only
        # starting in Python 3.11; older interpreters need the
        # ``+00:00`` substitution. Doing it unconditionally is safe.
        normalized = raw.replace("Z", "+00:00") if raw.endswith("Z") else raw
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError:
            return dt_or_str
    elif isinstance(dt_or_str, datetime):
        parsed = dt_or_str
    else:
        return str(dt_or_str)

    if parsed.tzinfo is None:
        # Same convention as ``datetime.utcnow()`` — every emitter in
        # the project hands us UTC, so a naive value means UTC.
        parsed = parsed.replace(tzinfo=timezone.utc)
    local = parsed.astimezone(_BEIJING_TZ)

    fmt = "%Y-%m-%d %H:%M:%S" if with_seconds else "%Y-%m-%d %H:%M"
    stamp = local.strftime(fmt)
    return f"{stamp} 北京时间" if with_label else stamp

INDEX_HEADER = """# 中国另类数据日报 — 历史归档

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
        lines.append("_暂无简报，请运行 `uv run cn-altdata-brief generate` 生成首份。_\n")
    else:
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
