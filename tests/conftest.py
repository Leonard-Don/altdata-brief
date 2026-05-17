"""Shared pytest fixtures."""

from __future__ import annotations

from pathlib import Path

import pytest

FIXTURE_ROOT = Path(__file__).parent / "fixtures"


@pytest.fixture
def super_pricing_cache() -> Path:
    return FIXTURE_ROOT / "super_pricing"


@pytest.fixture
def quant_trading_cache() -> Path:
    return FIXTURE_ROOT / "quant_trading"


@pytest.fixture
def index_research_tables() -> Path:
    return FIXTURE_ROOT / "index_research"


@pytest.fixture
def etf_512400_snapshot() -> Path:
    return FIXTURE_ROOT / "etf_512400" / "liveSnapshot.json"


@pytest.fixture
def all_adapter_paths(
    super_pricing_cache: Path,
    quant_trading_cache: Path,
    index_research_tables: Path,
    etf_512400_snapshot: Path,
) -> dict[str, Path]:
    return {
        "super_pricing": super_pricing_cache,
        "quant_trading": quant_trading_cache,
        "index_research": index_research_tables,
        "etf_512400": etf_512400_snapshot,
    }


@pytest.fixture
def patched_default_paths(
    monkeypatch: pytest.MonkeyPatch,
    super_pricing_cache: Path,
    quant_trading_cache: Path,
    index_research_tables: Path,
    etf_512400_snapshot: Path,
) -> None:
    """Point all adapter defaults at fixture dirs (shared by cli / validate / rss tests)."""
    from cn_altdata_brief.adapters import etf_512400 as etf_mod
    from cn_altdata_brief.adapters import index_research as ix_mod
    from cn_altdata_brief.adapters import quant_trading as qt_mod
    from cn_altdata_brief.adapters import super_pricing as sp_mod

    monkeypatch.setattr(sp_mod, "DEFAULT_CACHE_DIR", super_pricing_cache)
    monkeypatch.setattr(qt_mod, "DEFAULT_CACHE_DIR", quant_trading_cache)
    monkeypatch.setattr(ix_mod, "DEFAULT_TABLE_DIR", index_research_tables)
    monkeypatch.setattr(ix_mod, "DEFAULT_FIGURE_DIR", index_research_tables)
    monkeypatch.setattr(etf_mod, "DEFAULT_SNAPSHOT", etf_512400_snapshot)
