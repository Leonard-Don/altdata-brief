"""Adapter for ``quant-trading-system`` — industry heat + policy signal cache."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from cn_altdata_brief.adapters.base import AdapterBase, AdapterPayload, AdapterUnavailable

DEFAULT_ROOT = Path("/Users/leonardodon/PycharmProjects/quant-trading-system")
DEFAULT_CACHE_DIR = DEFAULT_ROOT / "cache" / "alt_data" / "providers"


class QuantTradingAdapter(AdapterBase):
    """Reads quant-trading's own copy of policy_radar.json + industry heat hints.

    For v0.1 the *industry heat* live endpoint is optional. When neither
    a live response nor a heat-cache file is available, the adapter
    derives a fallback ranking from the policy_radar industry_signals,
    so downstream synthesis always has something to render.
    """

    source_name = "quant-trading-system"
    live_url = "http://localhost:8000/api/v1/industry/industries/hot"

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
            },
        )


def _industry_heat_from_policy(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Derive a heat ranking from policy_radar industry_signals.

    Heat = mentions normalized to [0, 1] of the top mention count,
    blended with |avg_impact| as a secondary intensity proxy. This is
    a deliberate *fallback* that lets the brief render even when the
    upstream heat service is offline.
    """
    industry_signals: dict[str, dict[str, Any]] = (
        payload.get("signal", {}).get("industry_signals", {}) or {}
    )
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
