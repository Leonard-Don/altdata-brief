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
