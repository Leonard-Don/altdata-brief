"""Deterministic synthesis of the brief's 5 sections.

Each module returns a typed-ish dict ready for the Jinja template. The
synthesis layer is intentionally **rule-based, no LLM** in v0.2 — boring
and reliable beats clever and surprising for a daily research brief.
"""

from cn_altdata_brief.synthesis.etf_flow import synthesize_etf_flow
from cn_altdata_brief.synthesis.industry import synthesize_industry
from cn_altdata_brief.synthesis.inventory import synthesize_inventory
from cn_altdata_brief.synthesis.observation import synthesize_observation
from cn_altdata_brief.synthesis.policy import synthesize_policy

__all__ = [
    "synthesize_etf_flow",
    "synthesize_industry",
    "synthesize_inventory",
    "synthesize_observation",
    "synthesize_policy",
]
