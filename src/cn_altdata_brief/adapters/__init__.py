"""Adapters for each upstream data source.

Each adapter implements :class:`AdapterBase` and resolves data from
three possible sources, in priority order:

1. **live**: the source project's HTTP endpoint (opt-in via
   ``CN_ALTDATA_BRIEF_LIVE=1`` or ``--source-mode live``).
2. **public summary**: a sanitized JSON committed under the source
   repo's ``data/public/`` directory — the canonical input for
   GitHub Actions.
3. **cache**: the source project's on-disk caches (filesystem only;
   the legacy v0.1/v0.2 path).

See :mod:`cn_altdata_brief.config` for the preference toggling.
"""

from cn_altdata_brief.adapters.base import (
    AdapterBase,
    AdapterError,
    AdapterPayload,
    AdapterUnavailable,
    SourceResolution,
)
from cn_altdata_brief.adapters.etf_512400 import ETF512400Adapter
from cn_altdata_brief.adapters.index_research import IndexResearchAdapter
from cn_altdata_brief.adapters.quant_trading import QuantTradingAdapter
from cn_altdata_brief.adapters.super_pricing import SuperPricingAdapter
from cn_altdata_brief.config import SourceConfig, load_source_config

__all__ = [
    "AdapterBase",
    "AdapterError",
    "AdapterPayload",
    "AdapterUnavailable",
    "SourceResolution",
    "ETF512400Adapter",
    "IndexResearchAdapter",
    "QuantTradingAdapter",
    "SuperPricingAdapter",
    "build_default_adapters",
]


def build_default_adapters(
    *, config: SourceConfig | None = None
) -> dict[str, AdapterBase]:
    """Return the standard adapter bundle used by the CLI.

    Each adapter is constructed with its default path pointing at the
    sibling project layout on the maintainer's machine. Tests inject
    custom paths via the adapter constructors directly.

    v0.4: all four adapters now share the same ``config`` / source-mode
    contract. Quant-trading and ETF 512400 grew their public-summary
    branches in v0.4 (``quant_summary.json`` and the existing
    ``liveSnapshot.json`` aliased as a public artifact respectively).
    """
    cfg = config if config is not None else load_source_config()
    return {
        "super_pricing": SuperPricingAdapter(config=cfg),
        "quant_trading": QuantTradingAdapter(config=cfg),
        "index_research": IndexResearchAdapter(config=cfg),
        "etf_512400": ETF512400Adapter(config=cfg),
    }
