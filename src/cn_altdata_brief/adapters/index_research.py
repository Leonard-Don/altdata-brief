"""Adapter for ``index-inclusion-research`` — CMA verdicts + PAP guard.

Resolution order (set per-call via :class:`SourceConfig`)
--------------------------------------------------------

1. **Live endpoint** — not implemented for this source (file-based research).
2. **Public summary** — ``<source-repo>/data/public/index_research_summary.json``
   if present. Sanitized + versioned; works in GitHub Actions.
3. **CSV cache** — ``<source-repo>/results/real_tables/*.csv``. Local-only;
   the v0.1/v0.2 path.

Both #2 and #3 are mapped to the same internal expected shape:

* ``data.verdicts = [{"hid", "name_cn", "verdict", "confidence", "key_label",
                     "key_value", "p_value", "n_obs", "track",
                     "evidence_tier"}, ...]``
* ``data.pap_changes = [{"hid", "name_cn", "classification",
                         "baseline_verdict", "current_verdict", "notes"}, ...]``
* ``data.figure_links = [...]`` — paths to forest PNGs when present.
"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

from cn_altdata_brief.adapters.base import AdapterBase, AdapterPayload, AdapterUnavailable
from cn_altdata_brief.adapters.schema import SchemaContract, resolve_schema_version
from cn_altdata_brief.config import (
    SOURCE_REPO_DIRS,
    SourceConfig,
    public_summary_path,
)

DEFAULT_ROOT = SOURCE_REPO_DIRS["index_research"]
DEFAULT_TABLE_DIR = DEFAULT_ROOT / "results" / "real_tables"
DEFAULT_FIGURE_DIR = DEFAULT_ROOT / "results" / "figures"
DEFAULT_PUBLIC_SUMMARY = public_summary_path("index_research")

#: Public-summary schema versions this adapter can parse. Bump (and add a
#: parse branch in ``_parse_public_v*``) when index-inclusion-research
#: ships a new ``schema_version``. An unknown version raises loudly.
_SCHEMA = SchemaContract(source="index_research", supported=frozenset({1}))


class IndexResearchAdapter(AdapterBase):
    """Reads CMA verdicts + PAP deviation report from index-inclusion-research.

    Source preference is governed by :class:`SourceConfig` (env var
    ``CN_ALTDATA_BRIEF_PREFERENCE`` or ``--source-mode``).
    """

    source_name = "index-inclusion-research"
    live_url = None  # purely file-based — no HTTP endpoint

    def __init__(
        self,
        *,
        table_dir: Path | None = None,
        figure_dir: Path | None = None,
        public_summary: Path | None = None,
        allow_live: bool | None = None,
        config: SourceConfig | None = None,
    ) -> None:
        super().__init__(allow_live=allow_live)
        self.table_dir = Path(table_dir) if table_dir else DEFAULT_TABLE_DIR
        self.figure_dir = Path(figure_dir) if figure_dir else DEFAULT_FIGURE_DIR
        self.public_summary = (
            Path(public_summary) if public_summary else DEFAULT_PUBLIC_SUMMARY
        )
        self.config = self._resolve_config(
            config,
            cache_explicitly_set=table_dir is not None or figure_dir is not None,
            public_summary_explicitly_set=public_summary is not None,
            allow_live=allow_live,
        )

    # ------------------------------------------------------------------
    # Implementations
    # ------------------------------------------------------------------

    def _cache_probe_path(self) -> Path | None:
        return self.table_dir

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
                "source_mode": "cache",
            },
            files=figure_links,
        )

    def _load_from_public_summary(
        self, *, summary_path: Path | None = None
    ) -> AdapterPayload:
        """Read the sanitized public summary and map to internal shape.

        The committed schema (v1) is:

        * ``verdicts`` — either a list-of-rows OR a dict keyed by hid;
          each row carries ``name``/``name_cn``, ``verdict``,
          ``confidence``, ``evidence_tier``, ``track``, ``n_obs``, and
          a ``headline_metric`` string like ``"bootstrap p = 0.8748"``.
        * ``pap`` — optional list of rows (per-hypothesis pap deltas).
          When the upstream only ships ``pap_deviation_summary`` (counts),
          ``pap_changes`` becomes an empty list iff ``all_unchanged: true``.
        * ``sensitivity`` or ``sensitivity_robustness`` — surfaced as-is.
        * ``hs300_rdd`` — headline number; surfaced as-is on ``data.hs300_rdd``.
        * ``figures`` / ``figures_published`` — list of paths or basenames;
          resolved against ``self.figure_dir`` when possible.
        * ``generated_at`` / ``schema_version`` — provenance.

        Both list-of-rows and dict-of-rows verdict shapes are accepted to
        stay compatible with the index project's evolving schema.

        The version is resolved against :data:`_SCHEMA` *before* any
        nested field access — an unknown/newer ``schema_version`` raises
        :class:`~cn_altdata_brief.adapters.schema.UnsupportedSchemaVersionError`
        so the brief fails loud instead of shipping empty verdicts.
        """
        path = summary_path if summary_path is not None else self.public_summary
        payload = self.read_json(path)
        version = resolve_schema_version(payload, _SCHEMA)

        if version == 1:
            data, figure_links = _parse_public_v1(payload, self.figure_dir)
        else:  # pragma: no cover - resolve_schema_version guarantees v in {1}
            raise AssertionError(f"unhandled index_research schema_version={version}")

        data.update(
            {
                "verdicts_path": str(path),
                "pap_path": str(path),
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
            files=figure_links,
        )


# ----------------------------------------------------------------------
# Per-version parsers — one branch per supported schema_version.


def _parse_public_v1(
    payload: dict[str, Any], figure_dir: Path
) -> tuple[dict[str, Any], list[Path]]:
    """Parse a ``schema_version: 1`` index-research public summary.

    v1 shape::

        verdicts                — list[row] OR dict[hid -> row]
        pap                     — optional list[row]; else pap_deviation_summary
        sensitivity / sensitivity_robustness
        hs300_rdd               — headline RDD numbers
        figures / figures_published

    Returns ``(data, figure_links)`` — ``data`` is the internal dict
    minus provenance keys, ``figure_links`` is also stashed on
    ``AdapterPayload.files`` by the caller.
    """
    verdicts = _parse_verdicts_block(payload.get("verdicts"))
    pap_changes = _parse_pap_block(payload)
    figure_links = _resolve_figure_links(
        figure_dir,
        payload.get("figures"),
        payload.get("figures_published"),
    )
    data = {
        "verdicts": verdicts,
        "pap_changes": pap_changes,
        "figure_links": [str(p) for p in figure_links],
        "sensitivity": payload.get("sensitivity")
        or payload.get("sensitivity_robustness"),
        "hs300_rdd": payload.get("hs300_rdd"),
        "pap_baseline": payload.get("pap_baseline"),
        "pap_deviation_summary": payload.get("pap_deviation_summary"),
    }
    return data, figure_links


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


def _opt_float(v: Any) -> float | None:
    if v in (None, ""):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _opt_int(v: Any) -> int | None:
    if v in (None, ""):
        return None
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def _coerce_verdict_row(hid: str, raw: dict[str, Any]) -> dict[str, Any]:
    """Public summary already provides typed values; coerce defensively.

    ``raw`` may carry the row's hid implicitly (when the upstream uses a
    dict-of-rows shape) — the explicit ``hid`` argument always wins.

    Supports two source shapes:

    * **Cache-equivalent rows**: ``key_label`` / ``key_value`` / ``p_value``
      already broken out.
    * **Headline-string rows** (the format committed today): a single
      ``headline_metric`` like ``"bootstrap p = 0.8748"`` — we split on the
      last ``=`` and tag the prefix as label, the suffix as both
      ``key_value`` and (when label contains "p") ``p_value``.
    """
    name_cn = (
        raw.get("name_cn")
        if raw.get("name_cn")
        else raw.get("name", "")
    )
    key_label_raw = raw.get("key_label")
    key_value_raw: Any = raw.get("key_value")
    p_value_raw: Any = raw.get("p_value")
    headline = raw.get("headline_metric")
    if key_label_raw is None and isinstance(headline, str) and "=" in headline:
        label_part, _, value_part = headline.rpartition("=")
        key_label_raw = label_part.strip()
        key_value_raw = value_part.strip()
        # If label hints at a p-value, mirror to p_value.
        if "p" in key_label_raw.lower() and p_value_raw is None:
            p_value_raw = value_part.strip()

    return {
        "hid": str(hid or raw.get("hid", "")),
        "name_cn": str(name_cn or ""),
        "verdict": str(raw.get("verdict", "")),
        "confidence": str(raw.get("confidence", "")),
        "key_label": str(key_label_raw or ""),
        "key_value": _opt_float(key_value_raw),
        "p_value": _opt_float(p_value_raw),
        "n_obs": _opt_int(raw.get("n_obs")),
        "track": str(raw.get("track", "")),
        "evidence_tier": str(raw.get("evidence_tier", "")),
    }


def _coerce_pap_row(raw: dict[str, Any]) -> dict[str, Any]:
    return {
        "hid": str(raw.get("hid", "")),
        "name_cn": str(raw.get("name_cn", "")),
        "classification": str(raw.get("classification", "unchanged")),
        "baseline_verdict": str(raw.get("baseline_verdict", "")),
        "current_verdict": str(raw.get("current_verdict", "")),
        "notes": str(raw.get("notes", "")),
    }


def _parse_verdicts_block(block: Any) -> list[dict[str, Any]]:
    """Accept either ``list[row]`` or ``dict[hid -> row]`` and yield rows."""
    if isinstance(block, list):
        rows: list[dict[str, Any]] = []
        for raw in block:
            if isinstance(raw, dict):
                rows.append(_coerce_verdict_row(str(raw.get("hid", "")), raw))
        return rows
    if isinstance(block, dict):
        # Preserve hid order: insertion order is H1, H2, ... in practice.
        rows = []
        for hid, raw in block.items():
            if isinstance(raw, dict):
                rows.append(_coerce_verdict_row(str(hid), raw))
        return rows
    return []


def _parse_pap_block(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Either an explicit ``pap`` list-of-rows OR the summary-only shape.

    When the upstream only ships ``pap_deviation_summary`` (counts), we
    can't enumerate per-hypothesis rows. We then return:

    * empty list when ``all_unchanged: true`` (the brief's section will
      render the "no PAP changes" stable narrative).
    * a single synthetic row marked ``classification="changed"`` with
      placeholder hid/name when changes exist — the brief's downstream
      synthesizer reads the row count, not the row content.
    """
    raw_pap = payload.get("pap")
    if isinstance(raw_pap, list):
        rows = [_coerce_pap_row(r) for r in raw_pap if isinstance(r, dict)]
        return [r for r in rows if r["classification"] != "unchanged"]

    summary = payload.get("pap_deviation_summary") or {}
    if not isinstance(summary, dict):
        return []
    if summary.get("all_unchanged") is True:
        return []
    flipped = int(summary.get("flipped_count", 0) or 0)
    tightened = int(summary.get("tightened_count", 0) or 0)
    weakened = int(summary.get("weakened_count", 0) or 0)
    total_changed = flipped + tightened + weakened
    if total_changed <= 0:
        return []
    return [
        {
            "hid": "?",
            "name_cn": "(see upstream pap report)",
            "classification": "changed",
            "baseline_verdict": "",
            "current_verdict": "",
            "notes": (
                f"flipped={flipped}, tightened={tightened}, weakened={weakened} "
                "(detail not exposed in public summary)"
            ),
        }
        for _ in range(total_changed)
    ]


def _resolve_figure_links(
    figure_dir: Path,
    figures_basenames: Any,
    figures_published: Any,
) -> list[Path]:
    """Resolve a list of figure paths from either ``figures`` or
    ``figures_published`` keys.

    * ``figures`` carries plain basenames — joined with ``figure_dir``.
    * ``figures_published`` carries repo-relative paths (e.g.
      ``results/figures/foo.png``) — we strip the leading ``results/figures/``
      and join with ``figure_dir``.
    """
    links: list[Path] = []
    if isinstance(figures_basenames, list):
        for fname in figures_basenames:
            if not isinstance(fname, str):
                continue
            candidate = figure_dir / fname
            if candidate.exists():
                links.append(candidate)
    if isinstance(figures_published, list):
        for rel in figures_published:
            if not isinstance(rel, str):
                continue
            basename = Path(rel).name
            candidate = figure_dir / basename
            if candidate.exists() and candidate not in links:
                links.append(candidate)
    return links
