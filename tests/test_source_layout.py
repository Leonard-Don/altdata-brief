"""Source-repo sibling layout: flat ``<root>/<repo>``, no IDE-specific prefix.

Historically ``super_pricing`` / ``quant_trading`` carried a ``PycharmProjects/``
path segment (a leaked local-IDE layout) while ``index_research`` / ``etf_512400``
did not. That inconsistency meant the default layout matched neither a flat
sibling checkout nor the other two sources — so on any machine whose repos live
flat under ``~`` (or any ``ALTDATA_BRIEF_SOURCE_ROOT``), super-pricing's public
summary *and* narrative archive silently failed to resolve, dropping the brief
to constant baselines. This locks the flat convention so config, CI
(``daily.yml``), and ``scripts/smoke_e2e.sh`` stay in agreement.
"""

from __future__ import annotations

from altdata_brief.config import (
    DEFAULT_SOURCE_REPOS_ROOT,
    SOURCE_REPO_DIRS,
    narrative_history_path,
    public_summary_path,
)


def test_source_repo_dirs_use_flat_sibling_layout() -> None:
    expected = {
        "super_pricing": "super-pricing-system",
        "quant_trading": "quant-trading-system",
        "index_research": "index-inclusion-research",
    }
    for key, dirname in expected.items():
        path = SOURCE_REPO_DIRS[key]
        assert "PycharmProjects" not in path.parts, f"{key} still carries PycharmProjects: {path}"
        assert path == DEFAULT_SOURCE_REPOS_ROOT / dirname


def test_super_pricing_derived_paths_inherit_flat_layout() -> None:
    repo = DEFAULT_SOURCE_REPOS_ROOT / "super-pricing-system"
    assert public_summary_path("super_pricing") == repo / "data" / "public" / "alt_data_summary.json"
    assert narrative_history_path() == repo / "cache" / "alt_data" / "narrative_history.jsonl"
