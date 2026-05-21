"""Beijing-time formatting — a layer-neutral shared utility.

Lives at the package root so the render, synthesis and publish layers
can all use it without importing across layer boundaries.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

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


def format_beijing_time(
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
        parsed = parsed.replace(tzinfo=UTC)
    local = parsed.astimezone(_BEIJING_TZ)

    fmt = "%Y-%m-%d %H:%M:%S" if with_seconds else "%Y-%m-%d %H:%M"
    stamp = local.strftime(fmt)
    return f"{stamp} 北京时间" if with_label else stamp
