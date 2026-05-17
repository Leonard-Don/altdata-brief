"""Adapter for ``index-inclusion-research`` — CMA verdict tables + forest plots."""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

from cn_altdata_brief.adapters.base import AdapterBase, AdapterPayload, AdapterUnavailable

DEFAULT_ROOT = Path("/Users/leonardodon/index-inclusion-research")
DEFAULT_TABLE_DIR = DEFAULT_ROOT / "results" / "real_tables"
DEFAULT_FIGURE_DIR = DEFAULT_ROOT / "results" / "figures"


class IndexResearchAdapter(AdapterBase):
    """Reads CMA hypothesis verdicts + PAP deviation report.

    The brief's *observation* section uses verdict tier ("支持" / "部分
    支持" / "证据不足") plus PAP unchanged/changed flag to convey
    "what's holding up under fresh data".
    """

    source_name = "index-inclusion-research"
    live_url = None  # purely file-based — no HTTP endpoint

    def __init__(
        self,
        *,
        table_dir: Path | None = None,
        figure_dir: Path | None = None,
        allow_live: bool | None = None,
    ) -> None:
        super().__init__(allow_live=allow_live)
        self.table_dir = Path(table_dir) if table_dir else DEFAULT_TABLE_DIR
        self.figure_dir = Path(figure_dir) if figure_dir else DEFAULT_FIGURE_DIR

    # ------------------------------------------------------------------

    def fetch_cached(self) -> AdapterPayload:
        verdicts_path = self.table_dir / "cma_hypothesis_verdicts.csv"
        pap_path = self.table_dir / "pap_deviation_report.csv"

        if not verdicts_path.exists() and not pap_path.exists():
            raise AdapterUnavailable(
                f"neither {verdicts_path} nor {pap_path} exists; "
                "index-inclusion-research has no published verdicts yet"
            )

        verdicts = _read_verdicts(verdicts_path) if verdicts_path.exists() else []
        pap = _read_pap(pap_path) if pap_path.exists() else []

        # Find any forest PNG to link.
        figure_links: list[Path] = []
        if self.figure_dir.exists():
            for stem in (
                "cma_verdicts_forest.png",
                "cma_verdicts_sensitivity.png",
                "hs300_rdd_robustness_forest.png",
            ):
                candidate = self.figure_dir / stem
                if candidate.exists():
                    figure_links.append(candidate)

        return AdapterPayload(
            source=self.source_name,
            fetched_at=self.now_iso(),
            cache_path=verdicts_path if verdicts_path.exists() else pap_path,
            live=False,
            data={
                "verdicts": verdicts,
                "pap_changes": [r for r in pap if r["classification"] != "unchanged"],
                "verdicts_path": str(verdicts_path) if verdicts_path.exists() else None,
                "pap_path": str(pap_path) if pap_path.exists() else None,
                "figure_links": [str(p) for p in figure_links],
            },
            files=figure_links,
        )


# ----------------------------------------------------------------------


def _read_verdicts(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        for raw in reader:
            try:
                p_value = float(raw["p_value"]) if raw.get("p_value") else None
            except ValueError:
                p_value = None
            try:
                key_value = float(raw["key_value"]) if raw.get("key_value") else None
            except ValueError:
                key_value = None
            try:
                n_obs = int(float(raw["n_obs"])) if raw.get("n_obs") else None
            except ValueError:
                n_obs = None
            rows.append(
                {
                    "hid": raw.get("hid", ""),
                    "name_cn": raw.get("name_cn", ""),
                    "verdict": raw.get("verdict", ""),
                    "confidence": raw.get("confidence", ""),
                    "key_label": raw.get("key_label", ""),
                    "key_value": key_value,
                    "p_value": p_value,
                    "n_obs": n_obs,
                    "track": raw.get("track", ""),
                    "evidence_tier": raw.get("evidence_tier", ""),
                }
            )
    return rows


def _read_pap(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        for raw in reader:
            rows.append(
                {
                    "hid": raw.get("hid", ""),
                    "name_cn": raw.get("name_cn", ""),
                    "classification": raw.get("classification", "unchanged"),
                    "baseline_verdict": raw.get("baseline_verdict", ""),
                    "current_verdict": raw.get("current_verdict", ""),
                    "notes": raw.get("notes", ""),
                }
            )
    return rows
