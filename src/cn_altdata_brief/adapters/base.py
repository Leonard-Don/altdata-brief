"""Adapter base classes and shared protocols."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import requests

from cn_altdata_brief.config import SourceConfig, load_source_config

logger = logging.getLogger(__name__)


class AdapterError(RuntimeError):
    """Raised when an adapter hits a non-recoverable problem."""


class AdapterUnavailable(AdapterError):
    """Raised when a data source is unreachable.

    The synthesis layer catches this and degrades gracefully — it does
    NOT abort brief generation. A missing source becomes a "数据缺失"
    note in the relevant section.
    """


@dataclass
class AdapterPayload:
    """Normalized return type from every adapter.

    Attributes
    ----------
    source:
        Stable string identifier of the upstream project, e.g.
        ``"super-pricing-system"``.
    fetched_at:
        UTC ISO-8601 timestamp of when the payload was assembled.
    cache_path:
        Path to the on-disk cache file consulted (None when live mode).
    live:
        True if data came from a live HTTP endpoint, False if from cache.
    data:
        Adapter-specific dict, deliberately untyped here to keep the
        protocol minimal.
    files:
        Optional list of supplementary file paths the section may want
        to link to (e.g. forest-plot PNGs).
    """

    source: str
    fetched_at: str
    cache_path: Path | None
    live: bool
    data: dict[str, Any]
    files: list[Path] = field(default_factory=list)

    @property
    def cache_label(self) -> str:
        """Short label used in the per-section sources footer."""
        if self.live:
            return f"{self.source} (live)"
        if self.cache_path is None:
            return f"{self.source} (no cache)"
        return f"{self.source}::{self.cache_path.name}"


@dataclass(slots=True)
class SourceResolution:
    """Resolution metadata produced by :meth:`AdapterBase.resolve_source`.

    Returned by ``validate`` and surfaced in the CLI's per-adapter line so
    operators can see which on-disk path each adapter actually consulted.

    ``mode`` is one of ``"live"``, ``"public"``, ``"cache"``, or
    ``"missing"`` (no path could be resolved).
    ``path`` is the file path the adapter would read (or ``None`` for
    live and ``missing``).
    ``mtime_iso`` is the file mtime as ISO-8601 UTC, ``None`` when not
    on disk.
    ``available`` is True when fetch() should succeed.
    """

    source_name: str
    mode: str
    path: Path | None
    mtime_iso: str | None
    available: bool
    note: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source_name,
            "mode": self.mode,
            "path": str(self.path) if self.path is not None else None,
            "mtime": self.mtime_iso,
            "available": self.available,
            "note": self.note,
        }


class AdapterBase:
    """Common helpers for all adapters.

    Subclasses override :meth:`fetch_cached` (required) and optionally
    :meth:`fetch_live`. The public :meth:`fetch` method chooses live vs.
    cache based on the ``CN_ALTDATA_BRIEF_LIVE`` env var and the per-adapter
    ``live_url`` attribute.
    """

    source_name: str = "unknown"
    live_url: str | None = None

    def __init__(self, *, allow_live: bool | None = None) -> None:
        if allow_live is None:
            allow_live = os.environ.get("CN_ALTDATA_BRIEF_LIVE", "0") == "1"
        self.allow_live = allow_live

    def _resolve_config(
        self,
        config: SourceConfig | None,
        *,
        cache_explicitly_set: bool,
        public_summary_explicitly_set: bool,
        allow_live: bool | None,
    ) -> SourceConfig:
        """Return ``config`` if given, else derive one from constructor args.

        Subclass ``__init__``s call this instead of hand-rolling the
        ``load_source_config`` call. When the caller passed an explicit
        cache path but no public summary, the preference defaults to
        ``cache_only`` so ``Adapter(cache_dir=...)`` reads the cache
        directly (the long-standing test-construction shortcut).
        """
        if config is not None:
            return config
        preference = (
            "cache_only"
            if cache_explicitly_set and not public_summary_explicitly_set
            else None
        )
        return load_source_config(preference=preference, allow_live=allow_live)

    # -- public API ----------------------------------------------------------

    def fetch(self) -> AdapterPayload:
        """Resolve a payload via the live → public → cache preference order.

        The dispatch is shared by every adapter. Subclasses supply the
        data sources — :meth:`fetch_live`, :meth:`_load_from_public_summary`,
        :meth:`fetch_cached` — plus the ``config``, ``public_summary`` and
        ``live_url`` attributes set in their ``__init__``; they do not
        override ``fetch`` itself.

        ``AdapterUnavailable`` from the live or public step is swallowed
        so resolution falls through to the next source. Any other error
        (corrupt JSON, schema drift) propagates so the failure stays loud.
        """
        cfg = self.config
        if cfg.allow_live and self.live_url:
            try:
                return self.fetch_live()
            except AdapterUnavailable as exc:
                logger.warning(
                    "%s live fetch failed (%s); falling back", self.source_name, exc
                )
        if cfg.prefer_public and self.public_summary.exists():
            try:
                return self._load_from_public_summary()
            except AdapterUnavailable as exc:
                logger.info(
                    "%s public summary unavailable (%s); falling back to cache",
                    self.source_name,
                    exc,
                )
        if not cfg.allow_cache:
            raise AdapterUnavailable(self._public_only_missing_note())
        return self.fetch_cached()

    def _public_only_missing_note(self) -> str:
        """Message for the ``AdapterUnavailable`` raised when public-only
        mode finds no summary. Overridable for a source-specific hint.
        """
        return (
            f"{self.source_name}: public summary missing at "
            f"{self.public_summary}; source-mode=public forbids cache fallback"
        )

    def fetch_cached(self) -> AdapterPayload:  # pragma: no cover - abstract
        raise NotImplementedError

    def fetch_live(self) -> AdapterPayload:
        """Default live mode posts the cache contents wrapped as a live payload.

        Subclasses override when they have real endpoints to hit.
        """
        raise AdapterUnavailable(f"{self.source_name} has no live implementation")

    # ------------------------------------------------------------------
    # Resolution-only probe (no fetch / no normalization)
    # ------------------------------------------------------------------

    def resolve_source(self) -> SourceResolution:
        """Inspect which source would be used without actually fetching.

        v0.4 standardization: every adapter exposes this so the validate
        command (and any future "doctor" output) can report a uniform
        per-adapter line — *which* path the adapter resolved to, the
        file's mtime, and whether fetch would succeed.

        The default implementation handles the common case where the
        adapter holds three attributes (``live_url``, ``public_summary``,
        ``cache_dir`` or ``snapshot_path``). Subclasses with unusual
        layouts may override; the dataclass shape is the contract.

        This method MUST NOT raise — failures collapse to ``mode="missing"``
        with ``available=False``. The validate layer renders that to the
        user; raising would short-circuit the report for other adapters.
        """
        # Live mode wins when explicitly enabled. We don't probe HTTP here
        # (would be slow); we just declare the intent.
        cfg = getattr(self, "config", None)
        allow_live = bool(getattr(cfg, "allow_live", self.allow_live)) and bool(self.live_url)
        if allow_live:
            return SourceResolution(
                source_name=self.source_name,
                mode="live",
                path=None,
                mtime_iso=None,
                available=True,
                note=f"live endpoint {self.live_url}",
            )

        prefer_public = cfg is None or getattr(cfg, "prefer_public", True)
        allow_cache = cfg is None or getattr(cfg, "allow_cache", True)
        public_path = getattr(self, "public_summary", None)
        if prefer_public and isinstance(public_path, Path) and public_path.exists():
            return SourceResolution(
                source_name=self.source_name,
                mode="public",
                path=public_path,
                mtime_iso=self._mtime_iso(public_path),
                available=True,
            )

        cache_candidates: list[Path] = []
        for attr in ("snapshot_path", "cache_dir", "table_dir"):
            value = getattr(self, attr, None)
            if isinstance(value, Path):
                cache_candidates.append(value)
        cache_present = any(
            p.is_file() or (p.is_dir() and any(p.iterdir()))
            for p in cache_candidates
            if p.exists()
        )
        if cache_present and allow_cache:
            existing = next(p for p in cache_candidates if p.exists())
            return SourceResolution(
                source_name=self.source_name,
                mode="cache",
                path=existing,
                mtime_iso=self._mtime_iso(existing),
                available=True,
            )

        # Nothing resolved — but if public summary exists yet caller
        # forbade public (cache_only) AND we still have no cache, we land
        # here. Use a hint when we know the public path exists.
        note = None
        if isinstance(public_path, Path) and public_path.exists() and not prefer_public:
            note = "public summary exists but preference excludes it"
        elif not prefer_public and not allow_cache:
            note = "neither public nor cache permitted by current preference"
        return SourceResolution(
            source_name=self.source_name,
            mode="missing",
            path=None,
            mtime_iso=None,
            available=False,
            note=note,
        )

    @staticmethod
    def _mtime_iso(path: Path) -> str | None:
        try:
            ts = path.stat().st_mtime
        except OSError:
            return None
        return (
            datetime.fromtimestamp(ts, tz=UTC)
            .replace(microsecond=0)
            .isoformat()
        )

    # -- shared helpers ------------------------------------------------------

    @staticmethod
    def now_iso() -> str:
        return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")

    @staticmethod
    def read_json(path: Path) -> dict[str, Any]:
        if not path.exists():
            raise AdapterUnavailable(f"cache file missing: {path}")
        try:
            with path.open(encoding="utf-8") as fh:
                return json.load(fh)
        except json.JSONDecodeError as exc:
            raise AdapterError(f"cache file unreadable JSON: {path} ({exc})") from exc

    @staticmethod
    def http_get_json(url: str, timeout: float = 4.0) -> dict[str, Any]:
        try:
            resp = requests.get(url, timeout=timeout)
        except requests.RequestException as exc:
            raise AdapterUnavailable(f"GET {url} failed: {exc}") from exc
        if resp.status_code != 200:
            raise AdapterUnavailable(f"GET {url} returned HTTP {resp.status_code}")
        try:
            return resp.json()
        except ValueError as exc:
            raise AdapterError(f"non-JSON response from {url}") from exc
