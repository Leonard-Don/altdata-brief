"""Environment-aware paths and source-preference resolution for adapters.

Resolution policy
-----------------

Each adapter has THREE possible data sources, in priority order:

1. **Live endpoint** — set per-adapter ``<PROJECT>_LIVE_API`` env var (e.g.
   ``SUPER_PRICING_LIVE_API=http://localhost:8100``) to enable HTTP fetch.
2. **Public summary JSON** — sanitized, versioned artifact committed by
   the upstream project under ``data/public/<source>_summary.json``.
   This is the path GitHub Actions can use because the file is in git.
3. **Cache JSON / CSV** — internal cache that lives only on the
   maintainer's filesystem. Used as the final fallback so local
   development keeps working unchanged.

The mode is controlled by ``ALTDATA_BRIEF_PREFERENCE``. During the rename
from ``cn-altdata-brief``, legacy ``CN_ALTDATA_BRIEF_*`` variables are also
honored when the new names are unset. For compatibility with public-summary
fixture workflows, ``PUBLIC_SUMMARY_PREFERENCE`` remains the final alias:

* ``"auto"`` (default) — try (1), then (2), then (3); take whichever
  succeeds first.
* ``"public_only"`` — try (1), then (2). If both fail, raise
  ``AdapterUnavailable`` and DO NOT consult cache. This is what CI uses.
* ``"cache_only"`` — bypass (1) and (2), go straight to (3). Useful for
  reproducing historical briefs locally.

The CLI ``generate --source-mode {auto,public,cache,live}`` overrides
the env var for a single invocation.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

# ---------------------------------------------------------------------------
# Source-repo discovery
# ---------------------------------------------------------------------------

def env_value(name: str, legacy_name: str | None = None) -> str | None:
    """Read a renamed environment variable with one legacy fallback."""
    value = os.environ.get(name)
    if value is not None:
        return value
    if legacy_name is not None:
        return os.environ.get(legacy_name)
    return None


def env_flag(name: str, legacy_name: str | None = None, *, default: str = "0") -> bool:
    """Return True when a renamed boolean env var is set to ``"1"``."""
    return (env_value(name, legacy_name) or default) == "1"


#: Root directory where sibling source-repo checkouts live. Override via
#: ``ALTDATA_BRIEF_SOURCE_ROOT`` for CI workspaces. The legacy
#: ``CN_ALTDATA_BRIEF_SOURCE_ROOT`` name is accepted during migration.
DEFAULT_SOURCE_REPOS_ROOT = Path(
    env_value("ALTDATA_BRIEF_SOURCE_ROOT", "CN_ALTDATA_BRIEF_SOURCE_ROOT") or Path.home()
)

#: Sibling checkouts each adapter expects, laid out *flat* directly under
#: :data:`DEFAULT_SOURCE_REPOS_ROOT` (``<root>/<repo-dir>``). Names match
#: ``adapters/<key>.py``'s source-project identifier. Override the root via
#: ``ALTDATA_BRIEF_SOURCE_ROOT`` (CI sets it to ``<workspace>/sources``).
SOURCE_REPO_DIRS: dict[str, Path] = {
    "super_pricing": DEFAULT_SOURCE_REPOS_ROOT / "super-pricing-system",
    "quant_trading": DEFAULT_SOURCE_REPOS_ROOT / "quant-trading-system",
    "index_research": DEFAULT_SOURCE_REPOS_ROOT / "index-inclusion-research",
    "etf_512400": DEFAULT_SOURCE_REPOS_ROOT / "ETF 512400",
}

#: Public-summary filenames each source repo is expected to commit.
#:
#: v0.4 promotes ALL four source adapters to a public-summary path.
#:
#: * super_pricing: ``data/public/alt_data_summary.json`` (v0.3, committed).
#: * index_research: ``data/public/index_research_summary.json`` (v0.3, committed).
#: * quant_trading: ``data/public/quant_summary.json`` (v0.4, expected from
#:   a parallel agent dispatch; the adapter falls back to cache when absent).
#: * etf_512400: ``src/data/liveSnapshot.json`` — the JS app's snapshot is
#:   *intentionally* committed (the "protected dirty" pattern), so the
#:   on-disk artifact IS the public summary. The filename is non-standard
#:   on purpose — see :func:`public_summary_path` for the path override.
PUBLIC_SUMMARY_FILENAMES: dict[str, str] = {
    "super_pricing": "alt_data_summary.json",
    "index_research": "index_research_summary.json",
    "quant_trading": "quant_summary.json",
    "etf_512400": "liveSnapshot.json",
}

#: Per-source override of the ``data/public/`` subpath. By default the path
#: is ``<repo>/data/public/<filename>``; entries here override the prefix.
#: ETF 512400 ships its snapshot under ``src/data/`` for the JS app, so the
#: "public summary" lives there rather than under ``data/public/``.
_PUBLIC_SUMMARY_SUBPATH: dict[str, tuple[str, ...]] = {
    "etf_512400": ("src", "data"),
}


def public_summary_path(source_key: str, *, root: Path | None = None) -> Path:
    """Resolve where to look for ``<source>_summary.json``.

    ``root`` overrides the discovered source-repo root and is used by tests
    to point at an isolated tmp_path.

    The subpath defaults to ``data/public/`` but per-source overrides
    (see :data:`_PUBLIC_SUMMARY_SUBPATH`) are honored — used by the ETF
    adapter whose JS app commits the snapshot under ``src/data/``.
    """
    if source_key not in PUBLIC_SUMMARY_FILENAMES:
        raise KeyError(f"no public summary defined for source {source_key!r}")
    base = root if root is not None else SOURCE_REPO_DIRS.get(source_key, DEFAULT_SOURCE_REPOS_ROOT)
    subpath = _PUBLIC_SUMMARY_SUBPATH.get(source_key, ("data", "public"))
    leaf = PUBLIC_SUMMARY_FILENAMES[source_key]
    return base.joinpath(*subpath, leaf)


#: Relative location of super-pricing's narrative-history archive within its
#: repo. Unlike the public summary, this is a *local runtime cache* — it is
#: never committed and therefore absent in CI and fresh clones.
_NARRATIVE_HISTORY_SUBPATH: tuple[str, ...] = ("cache", "alt_data", "narrative_history.jsonl")


def narrative_history_path(*, root: Path | None = None) -> Path:
    """Resolve super-pricing's ``narrative_history.jsonl`` archive.

    ``root`` overrides the discovered super-pricing repo root (used by tests
    to point at an isolated ``tmp_path``). The archive is a local cache, so
    callers must treat a missing path as a normal fallback condition rather
    than an error.
    """
    base = root if root is not None else SOURCE_REPO_DIRS["super_pricing"]
    return base.joinpath(*_NARRATIVE_HISTORY_SUBPATH)


# ---------------------------------------------------------------------------
# Preference toggling
# ---------------------------------------------------------------------------

Preference = Literal["auto", "public_only", "cache_only"]

VALID_PREFERENCES: tuple[Preference, ...] = ("auto", "public_only", "cache_only")


@dataclass(frozen=True, slots=True)
class SourceConfig:
    """Resolution toggles, derived from env vars and CLI flags."""

    preference: Preference = "auto"
    allow_live: bool = False
    source_repos_root: Path = DEFAULT_SOURCE_REPOS_ROOT

    @property
    def prefer_public(self) -> bool:
        return self.preference in ("auto", "public_only")

    @property
    def allow_cache(self) -> bool:
        return self.preference in ("auto", "cache_only")

    @property
    def public_required(self) -> bool:
        return self.preference == "public_only"


def _normalize_preference(raw: str | None) -> Preference:
    if raw is None:
        return "auto"
    value = raw.strip().lower()
    if value in VALID_PREFERENCES:
        return value  # type: ignore[return-value]
    raise ValueError(
        f"invalid PUBLIC_SUMMARY_PREFERENCE={raw!r}; expected one of {VALID_PREFERENCES}"
    )


def load_source_config(
    *,
    preference: str | None = None,
    allow_live: bool | None = None,
) -> SourceConfig:
    """Build a ``SourceConfig`` from explicit args + environment variables.

    Explicit ``preference`` overrides ``ALTDATA_BRIEF_PREFERENCE``;
    ``ALTDATA_BRIEF_PREFERENCE`` overrides the legacy
    ``CN_ALTDATA_BRIEF_PREFERENCE``; both override the documented alias
    ``PUBLIC_SUMMARY_PREFERENCE``. Explicit ``allow_live`` overrides
    ``ALTDATA_BRIEF_LIVE`` and legacy ``CN_ALTDATA_BRIEF_LIVE``.
    """
    env_preference = env_value("ALTDATA_BRIEF_PREFERENCE", "CN_ALTDATA_BRIEF_PREFERENCE")
    alias_preference = os.environ.get("PUBLIC_SUMMARY_PREFERENCE")
    pref = _normalize_preference(preference or env_preference or alias_preference)
    if allow_live is None:
        allow_live = env_flag("ALTDATA_BRIEF_LIVE", "CN_ALTDATA_BRIEF_LIVE")
    return SourceConfig(
        preference=pref,
        allow_live=bool(allow_live),
        source_repos_root=DEFAULT_SOURCE_REPOS_ROOT,
    )


def source_mode_to_kwargs(source_mode: str) -> dict[str, object]:
    """Translate the CLI ``--source-mode`` flag to load_source_config kwargs.

    The CLI uses the short labels (``auto``, ``public``, ``cache``,
    ``live``) while the config layer uses the longer ``public_only`` /
    ``cache_only``.
    """
    mode = source_mode.strip().lower()
    if mode == "auto":
        return {"preference": "auto"}
    if mode == "public":
        return {"preference": "public_only"}
    if mode == "cache":
        return {"preference": "cache_only"}
    if mode == "live":
        return {"preference": "auto", "allow_live": True}
    raise ValueError(
        f"--source-mode must be one of auto/public/cache/live (got {source_mode!r})"
    )


__all__ = [
    "DEFAULT_SOURCE_REPOS_ROOT",
    "PUBLIC_SUMMARY_FILENAMES",
    "Preference",
    "SOURCE_REPO_DIRS",
    "SourceConfig",
    "VALID_PREFERENCES",
    "env_flag",
    "env_value",
    "load_source_config",
    "narrative_history_path",
    "public_summary_path",
    "source_mode_to_kwargs",
]
