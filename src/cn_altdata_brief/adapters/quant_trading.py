"""Adapter for ``quant-trading-system`` — industry heat + policy radar.

Resolution order (set per-call via :class:`SourceConfig`)
--------------------------------------------------------

1. **Live endpoint** — ``QUANT_TRADING_LIVE_API`` (or legacy
   ``CN_ALTDATA_BRIEF_LIVE=1``) → hit
   ``/api/v1/industry/industries/hot``.
2. **Public summary** — ``<source-repo>/data/public/quant_summary.json``
   if present. Sanitized + versioned; works in GitHub Actions.
3. **Cache JSON** — ``<source-repo>/cache/alt_data/providers/policy_radar.json``.
   Local-filesystem only; the v0.1/v0.2/v0.3 path.

v0.4 schema (public summary v1) mapped to internal shape::

    providers.policy_radar.top_industries           → derived industry heat list
    providers.industry_heat.top_industries_by_score → preferred heat list
    providers.etf_rotation                          → ETF rotation audit context
    providers.paper_trading                         → opt-in paper-trading meta

The internal expected shape (``data.industries``) is a sorted list of:
``{industry, heat_score, policy_signal, policy_impact, mentions}``. We
prefer ``industry_heat.top_industries_by_score`` when present, else fall
back to deriving from ``policy_radar.top_industries`` (with the same
mentions-normalized formula the cache path uses, so the brief reads the
same whether public or cache wins).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from cn_altdata_brief.adapters.base import AdapterBase, AdapterPayload, AdapterUnavailable
from cn_altdata_brief.adapters.schema import SchemaContract, resolve_schema_version
from cn_altdata_brief.config import (
    SOURCE_REPO_DIRS,
    SourceConfig,
    public_summary_path,
)

DEFAULT_ROOT = SOURCE_REPO_DIRS["quant_trading"]
DEFAULT_CACHE_DIR = DEFAULT_ROOT / "cache" / "alt_data" / "providers"
DEFAULT_PUBLIC_SUMMARY = public_summary_path("quant_trading")

#: Public-summary schema versions this adapter can parse. Bump (and add a
#: parse branch in ``_parse_public_v*``) when quant-trading-system ships a
#: new ``schema_version``. An unknown version raises loudly.
_SCHEMA = SchemaContract(source="quant_trading", supported=frozenset({1}))


class QuantTradingAdapter(AdapterBase):
    """Reads quant-trading's industry heat + policy_radar overlay.

    Source preference is governed by :class:`SourceConfig` (env var
    ``CN_ALTDATA_BRIEF_PREFERENCE`` or ``--source-mode``).

    For v0.1–v0.3 only the cache path was implemented; v0.4 adds the
    public summary path (``data/public/quant_summary.json``) so the
    GitHub Actions workflow can read this source without checking out
    the entire upstream repo.

    When neither live nor public is available — or when neither contains
    a ``heat`` ranking — the adapter derives a fallback ranking from
    ``policy_radar.industry_signals`` so the brief always renders.
    """

    source_name = "quant-trading-system"
    live_url = os.environ.get(
        "QUANT_TRADING_LIVE_API",
        "http://localhost:8000/api/v1/industry/industries/hot",
    )

    def __init__(
        self,
        *,
        cache_dir: Path | None = None,
        public_summary: Path | None = None,
        allow_live: bool | None = None,
        config: SourceConfig | None = None,
    ) -> None:
        super().__init__(allow_live=allow_live)
        self.cache_dir = Path(cache_dir) if cache_dir else DEFAULT_CACHE_DIR
        self.public_summary = (
            Path(public_summary) if public_summary else DEFAULT_PUBLIC_SUMMARY
        )
        self.config = self._resolve_config(
            config,
            cache_explicitly_set=cache_dir is not None,
            public_summary_explicitly_set=public_summary is not None,
            allow_live=allow_live,
        )

    # ------------------------------------------------------------------
    # Implementations
    # ------------------------------------------------------------------

    def fetch_cached(self) -> AdapterPayload:
        policy_path = self.cache_dir / "policy_radar.json"
        if not policy_path.exists():
            raise AdapterUnavailable(
                f"policy_radar.json not found at {policy_path}; "
                "quant-trading-system cache is empty"
            )

        payload = self.read_json(policy_path)
        industries = _industry_heat_from_policy(payload)
        return AdapterPayload(
            source=self.source_name,
            fetched_at=self.now_iso(),
            cache_path=policy_path,
            live=False,
            data={
                "industries": industries,
                "policy_count": int(
                    payload.get("signal", {}).get("policy_count", 0) or 0
                ),
                "policy_timestamp": payload.get("signal", {}).get("timestamp"),
                "cache_path": str(policy_path),
                "source_mode": "cache",
            },
        )

    def fetch_live(self) -> AdapterPayload:
        if not self.live_url:  # pragma: no cover
            raise AdapterUnavailable("quant-trading live URL not configured")
        raw = self.http_get_json(self.live_url + "?include_policy_signal=true")
        # Live endpoint contract: list of industries with policy overlay.
        rows: list[dict[str, Any]] = []
        for row in raw.get("data", []) or []:
            rows.append(
                {
                    "industry": row.get("name") or row.get("industry"),
                    "heat_score": float(row.get("heat", 0.0) or 0.0),
                    "policy_signal": row.get("policy_signal", "neutral"),
                    "policy_impact": float(row.get("policy_impact", 0.0) or 0.0),
                    "mentions": int(row.get("mentions", 0) or 0),
                }
            )
        return AdapterPayload(
            source=self.source_name,
            fetched_at=self.now_iso(),
            cache_path=None,
            live=True,
            data={
                "industries": rows,
                "policy_count": int(raw.get("policy_count", 0) or 0),
                "policy_timestamp": raw.get("timestamp"),
                "source_mode": "live",
            },
        )

    def _load_from_public_summary(
        self, *, summary_path: Path | None = None
    ) -> AdapterPayload:
        """Read the sanitized public summary and map to internal shape.

        The expected schema (v1, ``schema_version`` key)::

            {
              "schema_version": 1,
              "generated_at": "...",
              "providers": {
                "policy_radar": {
                  "policy_count": int,
                  "last_refresh_at": "...",
                  "top_industries": [
                    {"industry": str, "avg_impact": float,
                     "mentions": int, "signal": str}, ...
                  ]
                },
                "industry_heat": {
                  "top_industries_by_score": [
                    {"industry": str, "heat_score": float,
                     "policy_signal": str, "policy_impact": float,
                     "mentions": int}, ...
                  ]
                },
                "etf_rotation": {
                  "audit_count": int, "strategy_count": int,
                  ...
                },
                "paper_trading": {...}   # optional
              }
            }

        When ``industry_heat.top_industries_by_score`` is present we
        prefer it (the upstream's real heat ranking). When only
        ``policy_radar.top_industries`` exists, we derive heat the same
        way the cache path does, so the brief reads identical bullets.

        The version is resolved against :data:`_SCHEMA` *before* any
        nested field access — an unknown/newer ``schema_version`` raises
        :class:`~cn_altdata_brief.adapters.schema.UnsupportedSchemaVersionError`
        so the brief fails loud instead of deriving an empty ranking.
        """
        path = summary_path if summary_path is not None else self.public_summary
        payload = self.read_json(path)
        version = resolve_schema_version(payload, _SCHEMA)

        if version == 1:
            data = _parse_public_v1(payload)
        else:  # pragma: no cover - resolve_schema_version guarantees v in {1}
            raise AssertionError(f"unhandled quant_trading schema_version={version}")

        data.update(
            {
                "public_summary_path": str(path),
                "cache_path": str(path),
                "schema_version": version,
                "generated_at": payload.get("generated_at"),
                "source_mode": "public",
            }
        )
        return AdapterPayload(
            source=self.source_name,
            fetched_at=self.now_iso(),
            cache_path=path,
            live=False,
            data=data,
        )


# ----------------------------------------------------------------------
# Per-version parsers — one branch per supported schema_version.


def _parse_public_v1(payload: dict[str, Any]) -> dict[str, Any]:
    """Parse a ``schema_version: 1`` quant-trading public summary.

    v1 shape::

        providers.policy_radar.{policy_count, last_refresh_at, top_industries}
        providers.industry_heat.top_industries_by_score   — authoritative heat
        providers.etf_rotation.{audit_count, strategy_count, last_refresh_at}
        providers.paper_trading.{...}                      — optional

    Returns the internal ``data`` dict (without provenance keys — the
    caller layers ``schema_version`` / ``source_mode`` on top).
    """
    providers = payload.get("providers", {}) or {}
    policy_block = providers.get("policy_radar", {}) or {}
    heat_block = providers.get("industry_heat", {}) or {}
    rotation_block = providers.get("etf_rotation", {}) or {}
    paper_block = providers.get("paper_trading", {}) or {}

    return {
        "industries": _industries_from_public(policy_block, heat_block),
        "policy_count": int(policy_block.get("policy_count", 0) or 0),
        "policy_timestamp": policy_block.get("last_refresh_at"),
        "etf_rotation": {
            "audit_count": int(rotation_block.get("audit_count", 0) or 0),
            "strategy_count": int(rotation_block.get("strategy_count", 0) or 0),
            "last_refresh_at": rotation_block.get("last_refresh_at"),
        },
        "paper_trading": paper_block or None,
    }


# ----------------------------------------------------------------------


def _industries_from_public(
    policy_block: dict[str, Any],
    heat_block: dict[str, Any],
) -> list[dict[str, Any]]:
    """Prefer the upstream's explicit heat ranking when present.

    The brief only needs ``industries`` sorted by heat_score desc; we
    accept either an authoritative ``top_industries_by_score`` from
    ``industry_heat`` or, as a fallback, derive heat from
    ``policy_radar.top_industries`` using the same mentions-normalized
    formula the cache path uses.
    """
    explicit = heat_block.get("top_industries_by_score") or []
    if isinstance(explicit, list) and explicit:
        rows: list[dict[str, Any]] = []
        for row in explicit:
            if not isinstance(row, dict):
                continue
            rows.append(
                {
                    "industry": str(row.get("industry") or row.get("name") or "未知"),
                    "heat_score": float(row.get("heat_score", 0.0) or 0.0),
                    "policy_signal": str(row.get("policy_signal", "neutral")),
                    "policy_impact": float(row.get("policy_impact", 0.0) or 0.0),
                    "mentions": int(row.get("mentions", 0) or 0),
                }
            )
        rows.sort(key=lambda r: r["heat_score"], reverse=True)
        if rows:
            return rows

    # Fallback: derive heat from policy_radar's top_industries.
    top_industries = policy_block.get("top_industries") or []
    if isinstance(top_industries, list) and top_industries:
        as_dict = {
            str(row.get("industry") or row.get("name") or f"row_{i}"): {
                "avg_impact": row.get("avg_impact", 0.0),
                "mentions": row.get("mentions", 0),
                "signal": row.get("signal", "neutral"),
            }
            for i, row in enumerate(top_industries)
            if isinstance(row, dict)
        }
        return _heat_from_industry_signals(as_dict)

    # Last resort: the upstream may have shipped industry_signals as a
    # dict (mirroring the cache shape).
    signals = policy_block.get("industry_signals") or {}
    if isinstance(signals, dict) and signals:
        return _heat_from_industry_signals(signals)
    return []


def _industry_heat_from_policy(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Derive a heat ranking from policy_radar industry_signals (cache shape).

    Heat = mentions normalized to [0, 1] of the top mention count,
    blended with |avg_impact| as a secondary intensity proxy. This is
    a deliberate *fallback* that lets the brief render even when the
    upstream heat service is offline.
    """
    industry_signals: dict[str, dict[str, Any]] = (
        payload.get("signal", {}).get("industry_signals", {}) or {}
    )
    return _heat_from_industry_signals(industry_signals)


def _heat_from_industry_signals(
    industry_signals: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Shared heat formula used by both cache and public-summary fallback."""
    if not industry_signals:
        return []
    max_mentions = max(int(info.get("mentions", 0) or 0) for info in industry_signals.values())
    max_mentions = max(max_mentions, 1)  # avoid div-by-zero
    rows: list[dict[str, Any]] = []
    for name, info in industry_signals.items():
        mentions = int(info.get("mentions", 0) or 0)
        impact = float(info.get("avg_impact", 0.0) or 0.0)
        heat = round(0.6 * (mentions / max_mentions) + 0.4 * min(abs(impact), 1.0), 4)
        rows.append(
            {
                "industry": name,
                "heat_score": heat,
                "policy_signal": str(info.get("signal", "neutral")),
                "policy_impact": impact,
                "mentions": mentions,
            }
        )
    rows.sort(key=lambda r: r["heat_score"], reverse=True)
    return rows
