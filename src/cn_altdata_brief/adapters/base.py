"""Adapter base classes and shared protocols."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import requests

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

    # -- public API ----------------------------------------------------------

    def fetch(self) -> AdapterPayload:
        """Return a payload, preferring live mode when enabled."""
        if self.allow_live and self.live_url:
            try:
                return self.fetch_live()
            except AdapterUnavailable as exc:
                logger.warning("%s live fetch failed (%s); falling back to cache", self.source_name, exc)
        return self.fetch_cached()

    def fetch_cached(self) -> AdapterPayload:  # pragma: no cover - abstract
        raise NotImplementedError

    def fetch_live(self) -> AdapterPayload:
        """Default live mode posts the cache contents wrapped as a live payload.

        Subclasses override when they have real endpoints to hit.
        """
        raise AdapterUnavailable(f"{self.source_name} has no live implementation")

    # -- shared helpers ------------------------------------------------------

    @staticmethod
    def now_iso() -> str:
        return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"

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
