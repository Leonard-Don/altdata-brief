"""Adapter for ``super-pricing-system`` — policy radar + macro HF.

Resolution order (set per-call via :class:`SourceConfig`)
--------------------------------------------------------

1. **Live endpoint** — ``SUPER_PRICING_LIVE_API`` (or legacy
   ``CN_ALTDATA_BRIEF_LIVE=1``) → hit ``/api/v1/alt-data/narrative``.
2. **Public summary** — ``<source-repo>/data/public/alt_data_summary.json``
   if present. Sanitized + versioned; works in GitHub Actions.
3. **Cache JSON** — ``<source-repo>/cache/alt_data/providers/*.json``.
   Local-filesystem only; the v0.1/v0.2 path.

Both #2 and #3 are mapped to the same internal expected shape:

* ``data.policy_radar = {"industry_signals": [...], "policy_count", "signal_score", ...}``
* ``data.macro_hf = {"metals": [...], "ports": {...} | None, "timestamp": ...}``
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

DEFAULT_ROOT = SOURCE_REPO_DIRS["super_pricing"]
DEFAULT_CACHE_DIR = DEFAULT_ROOT / "cache" / "alt_data" / "providers"
DEFAULT_PUBLIC_SUMMARY = public_summary_path("super_pricing")

#: Public-summary schema versions this adapter can parse. Bump (and add a
#: parse branch in ``_parse_public_v*``) when super-pricing-system ships a
#: new ``schema_version``. An unknown version raises loudly.
_SCHEMA = SchemaContract(source="super_pricing", supported=frozenset({1}))


class SuperPricingAdapter(AdapterBase):
    """Reads policy_radar + macro_hf from super-pricing-system.

    Source preference is governed by :class:`SourceConfig` (env var
    ``CN_ALTDATA_BRIEF_PREFERENCE`` or ``--source-mode``).
    """

    source_name = "super-pricing-system"
    live_url = os.environ.get(
        "SUPER_PRICING_LIVE_API",
        "http://localhost:8100/api/v1/alt-data/narrative",
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

    def _cache_probe_path(self) -> Path | None:
        return self.cache_dir

    def fetch_cached(self) -> AdapterPayload:
        policy_path = self.cache_dir / "policy_radar.json"
        macro_path = self.cache_dir / "macro_hf.json"

        if not policy_path.exists() and not macro_path.exists():
            raise AdapterUnavailable(
                f"neither {policy_path} nor {macro_path} exists; "
                "check the super-pricing-system project location"
            )

        policy_payload: dict[str, Any] = (
            self.read_json(policy_path) if policy_path.exists() else {}
        )
        macro_payload: dict[str, Any] = (
            self.read_json(macro_path) if macro_path.exists() else {}
        )

        return AdapterPayload(
            source=self.source_name,
            fetched_at=self.now_iso(),
            cache_path=policy_path if policy_path.exists() else macro_path,
            live=False,
            data={
                "policy_radar": _normalize_policy(policy_payload),
                "macro_hf": _normalize_macro(macro_payload),
                "policy_cache_path": str(policy_path) if policy_path.exists() else None,
                "macro_cache_path": str(macro_path) if macro_path.exists() else None,
                "source_mode": "cache",
            },
        )

    def fetch_live(self) -> AdapterPayload:
        if not self.live_url:  # pragma: no cover - guarded by allow_live
            raise AdapterUnavailable("super-pricing live URL not configured")
        raw = self.http_get_json(self.live_url)
        # The live endpoint returns a synthesized narrative; we still want the
        # underlying signals, so we layer the cached structure when present.
        try:
            base = self.fetch_cached()
        except AdapterUnavailable:
            # Public summary as next-best layered structure.
            if self.public_summary.exists():
                base = self._load_from_public_summary()
            else:
                raise
        base.data["narrative_live"] = raw
        base.live = True
        base.data["source_mode"] = "live"
        return base

    def _load_from_public_summary(
        self, *, summary_path: Path | None = None
    ) -> AdapterPayload:
        """Read the sanitized public summary, routing on ``schema_version``.

        The version is resolved against :data:`_SCHEMA` *before* any
        nested field access. An unknown/newer version raises
        :class:`~cn_altdata_brief.adapters.schema.UnsupportedSchemaVersionError`
        — the adapter refuses to guess a shape it was not written for,
        which is the loud-failure contract (no silent all-zeros brief).
        """
        path = summary_path if summary_path is not None else self.public_summary
        payload = self.read_json(path)
        version = resolve_schema_version(payload, _SCHEMA)

        if version == 1:
            data = _parse_public_v1(payload)
        else:  # pragma: no cover - resolve_schema_version guarantees v in {1}
            raise AssertionError(f"unhandled super_pricing schema_version={version}")

        data.update(
            {
                "policy_cache_path": str(path),
                "macro_cache_path": str(path),
                "public_summary_path": str(path),
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
    """Parse a ``schema_version: 1`` super-pricing public summary.

    v1 shape::

        providers.policy_radar.industry_signals   — dict[name -> info]
        providers.policy_radar.policy_count / last_refresh_at
        providers.macro_hf.metals.<metal>.weekly_change_pct / trend
            — flat per-metal dict (NO ``records[].raw_value`` array)
        providers.macro_hf.macro_pressure etc.

    Returns the internal ``data`` dict (without provenance keys — the
    caller layers ``schema_version`` / ``source_mode`` on top).
    """
    providers = payload.get("providers", {}) or {}
    policy_block = providers.get("policy_radar", {}) or {}
    macro_block = providers.get("macro_hf", {}) or {}
    return {
        "policy_radar": _normalize_policy_from_public(policy_block),
        "macro_hf": _normalize_macro_from_public(macro_block),
    }


# ----------------------------------------------------------------------
# Normalizers — keep the synthesis layer dialect-free.


def _normalize_policy(payload: dict[str, Any]) -> dict[str, Any]:
    """Extract the bits the policy section actually needs (cache shape)."""
    if not payload:
        return {"industry_signals": {}, "policy_count": 0, "signal_score": None, "timestamp": None}
    signal_block = payload.get("signal", {}) or {}
    industry_signals = signal_block.get("industry_signals", {}) or {}
    return {
        "industry_signals": _rank_industry_signals(industry_signals),
        "policy_count": int(signal_block.get("policy_count", 0) or 0),
        "signal_score": signal_block.get("score"),
        "confidence": signal_block.get("confidence"),
        "timestamp": signal_block.get("timestamp"),
        "source_health": signal_block.get("source_health", {}),
    }


def _normalize_policy_from_public(block: dict[str, Any]) -> dict[str, Any]:
    """Map ``providers.policy_radar`` (public summary v1) to internal shape."""
    if not block:
        return {"industry_signals": {}, "policy_count": 0, "signal_score": None, "timestamp": None}
    industry_signals = block.get("industry_signals", {}) or {}
    return {
        "industry_signals": _rank_industry_signals(industry_signals),
        "policy_count": int(block.get("policy_count", 0) or 0),
        # public summary doesn't expose raw score/confidence; we leave them
        # None rather than fabricate.
        "signal_score": None,
        "confidence": None,
        "timestamp": block.get("last_refresh_at"),
        "source_health": {},
    }


def _rank_industry_signals(industry_signals: dict[str, Any]) -> list[dict[str, Any]]:
    """Common ranking step — sorted by |avg_impact| desc, mentions desc."""
    return sorted(
        (
            {
                "industry": name,
                "avg_impact": float(info.get("avg_impact", 0.0) or 0.0),
                "mentions": int(info.get("mentions", 0) or 0),
                "signal": str(info.get("signal", "neutral")),
            }
            for name, info in industry_signals.items()
        ),
        key=lambda r: (abs(r["avg_impact"]), r["mentions"]),
        reverse=True,
    )


def _normalize_macro(payload: dict[str, Any]) -> dict[str, Any]:
    """Pull metal-level inventory rows out of macro_hf.json records (cache)."""
    if not payload:
        return {"metals": [], "ports": None, "timestamp": None}
    records = payload.get("records", []) or []
    metals: list[dict[str, Any]] = []
    ports: dict[str, Any] | None = None
    for rec in records:
        raw = rec.get("raw_value", {}) or {}
        data_type = raw.get("data_type")
        if data_type == "inventory":
            metals.append(
                {
                    "metal": raw.get("metal"),
                    "name_cn": raw.get("name"),
                    "trend": raw.get("trend"),
                    "price_change_pct": float(raw.get("price_change_pct", 0.0) or 0.0),
                    "volatility": float(raw.get("volatility", 0.0) or 0.0),
                    "confidence": float(raw.get("confidence", 0.0) or 0.0),
                    "source_mode": raw.get("source_mode"),
                }
            )
        elif data_type == "ports":
            ports = {
                "global_index": float(raw.get("global_index", 0.0) or 0.0),
                "status": raw.get("status"),
                "tracked_ports": int(raw.get("tracked_ports", 0) or 0),
                "coverage": float(raw.get("coverage", 0.0) or 0.0),
            }
    # Deduplicate metals by (metal name) — keep first occurrence.
    seen: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for m in metals:
        key = m.get("metal") or m.get("name_cn") or ""
        if key in seen:
            continue
        seen.add(key)
        deduped.append(m)
    return {
        "metals": deduped,
        "ports": ports,
        "timestamp": payload.get("signal", {}).get("timestamp"),
    }


# Chinese names for metals — the public summary uses the English key only,
# so the brief looks consistent we look up a name_cn when possible.
_METAL_NAME_CN: dict[str, str] = {
    "copper": "铜",
    "aluminium": "铝",
    "aluminum": "铝",
    "nickel": "镍",
    "zinc": "锌",
    "lead": "铅",
    "tin": "锡",
    "iron_ore": "铁矿石",
    "steel": "钢",
}


def _normalize_macro_from_public(block: dict[str, Any]) -> dict[str, Any]:
    """Map ``providers.macro_hf`` (public summary v1) to internal shape.

    Public schema is flatter than the cache — no ``records[]`` array.
    ``metals`` is a dict keyed by english name, each carrying
    ``trend`` and ``weekly_change_pct``. Region/confidence detail
    lives under ``region_breakdown.<region>.confidence`` — we pick the
    first region's confidence (deterministic via dict ordering).
    """
    if not block:
        return {"metals": [], "ports": None, "timestamp": None}
    metals_block = block.get("metals", {}) or {}
    metals: list[dict[str, Any]] = []
    for metal_key, info in metals_block.items():
        if not isinstance(info, dict):
            continue
        region_breakdown = info.get("region_breakdown", {}) or {}
        # Pick first region's confidence/source_mode deterministically.
        confidence_val = 0.0
        source_mode_val: str | None = None
        for _region_name, region_info in region_breakdown.items():
            if not isinstance(region_info, dict):
                continue
            confidence_val = float(region_info.get("confidence", 0.0) or 0.0)
            source_mode_val = region_info.get("source_mode")
            break
        metals.append(
            {
                "metal": metal_key,
                "name_cn": _METAL_NAME_CN.get(metal_key, metal_key),
                "trend": info.get("trend"),
                "price_change_pct": float(info.get("weekly_change_pct", 0.0) or 0.0),
                # Public summary doesn't expose per-metal volatility; leave 0.
                "volatility": 0.0,
                "confidence": confidence_val,
                "source_mode": source_mode_val,
            }
        )

    # Public summary doesn't include port congestion under macro_hf as of v1.
    # If a later schema adds it, surface it here.
    ports: dict[str, Any] | None = None
    ports_block = block.get("ports") or {}
    if isinstance(ports_block, dict) and ports_block:
        ports = {
            "global_index": float(ports_block.get("global_index", 0.0) or 0.0),
            "status": ports_block.get("status"),
            "tracked_ports": int(ports_block.get("tracked_ports", 0) or 0),
            "coverage": float(ports_block.get("coverage", 0.0) or 0.0),
        }
    return {
        "metals": metals,
        "ports": ports,
        "timestamp": block.get("last_refresh_at"),
    }
