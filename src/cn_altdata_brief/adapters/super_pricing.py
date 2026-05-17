"""Adapter for ``super-pricing-system`` — policy radar + macro HF cache."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from cn_altdata_brief.adapters.base import AdapterBase, AdapterPayload, AdapterUnavailable

DEFAULT_ROOT = Path("/Users/leonardodon/PycharmProjects/super-pricing-system")
DEFAULT_CACHE_DIR = DEFAULT_ROOT / "cache" / "alt_data" / "providers"


class SuperPricingAdapter(AdapterBase):
    """Reads the cached provider JSONs from super-pricing-system.

    The brief consumes ``policy_radar.json`` (policy section) and
    ``macro_hf.json`` (inventory section). Both files are present in
    the source project's cache directory under
    ``cache/alt_data/providers/``.
    """

    source_name = "super-pricing-system"
    live_url = "http://localhost:8100/api/v1/alt-data/narrative"

    def __init__(
        self,
        *,
        cache_dir: Path | None = None,
        allow_live: bool | None = None,
    ) -> None:
        super().__init__(allow_live=allow_live)
        self.cache_dir = Path(cache_dir) if cache_dir else DEFAULT_CACHE_DIR

    # ------------------------------------------------------------------

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
            },
        )

    def fetch_live(self) -> AdapterPayload:
        if not self.live_url:  # pragma: no cover - guarded by allow_live
            raise AdapterUnavailable("super-pricing live URL not configured")
        raw = self.http_get_json(self.live_url)
        # The live endpoint returns a synthesized narrative; we still want the
        # underlying signals, so we layer the cached structure when present.
        cached = self.fetch_cached()
        cached.data["narrative_live"] = raw
        cached.live = True
        return cached


# ----------------------------------------------------------------------
# Normalizers — keep the synthesis layer dialect-free.


def _normalize_policy(payload: dict[str, Any]) -> dict[str, Any]:
    """Extract the bits the policy section actually needs."""
    if not payload:
        return {"industry_signals": {}, "policy_count": 0, "signal_score": None, "timestamp": None}
    signal_block = payload.get("signal", {}) or {}
    industry_signals = signal_block.get("industry_signals", {}) or {}
    # Sort industries by |avg_impact| desc, mentions desc as tiebreak.
    ranked = sorted(
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
    return {
        "industry_signals": ranked,
        "policy_count": int(signal_block.get("policy_count", 0) or 0),
        "signal_score": signal_block.get("score"),
        "confidence": signal_block.get("confidence"),
        "timestamp": signal_block.get("timestamp"),
        "source_health": signal_block.get("source_health", {}),
    }


def _normalize_macro(payload: dict[str, Any]) -> dict[str, Any]:
    """Pull metal-level inventory rows out of macro_hf.json records."""
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
