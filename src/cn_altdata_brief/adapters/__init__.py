"""Adapters for each upstream data source.

Each adapter implements :class:`AdapterBase` and supports two modes:

* **cached**: read JSON / CSV from the source project's on-disk artifacts.
* **live**: hit the source project's HTTP endpoint when available.

The brief generator is cache-first; live mode is opt-in via the
``CN_ALTDATA_BRIEF_LIVE`` environment variable.
"""

from cn_altdata_brief.adapters.base import (
    AdapterBase,
    AdapterError,
    AdapterPayload,
    AdapterUnavailable,
)
from cn_altdata_brief.adapters.etf_512400 import ETF512400Adapter
from cn_altdata_brief.adapters.index_research import IndexResearchAdapter
from cn_altdata_brief.adapters.quant_trading import QuantTradingAdapter
from cn_altdata_brief.adapters.super_pricing import SuperPricingAdapter

__all__ = [
    "AdapterBase",
    "AdapterError",
    "AdapterPayload",
    "AdapterUnavailable",
    "ETF512400Adapter",
    "IndexResearchAdapter",
    "QuantTradingAdapter",
    "SuperPricingAdapter",
    "build_default_adapters",
]


def build_default_adapters() -> dict[str, AdapterBase]:
    """Return the standard adapter bundle used by the CLI.

    Each adapter is constructed with its default path pointing at the
    sibling project layout on the maintainer's machine. Tests inject
    custom paths via the adapter constructors directly.
    """
    return {
        "super_pricing": SuperPricingAdapter(),
        "quant_trading": QuantTradingAdapter(),
        "index_research": IndexResearchAdapter(),
        "etf_512400": ETF512400Adapter(),
    }
