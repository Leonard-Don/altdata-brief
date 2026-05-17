"""v0.7 — optional LLM rephrase layer for the 本日观察 section.

The deterministic synthesis pipeline (v0.2) is the *ground truth*; this
package adds a side-channel that rewrites the rule-based prose into a
more journalistic Chinese register without changing any facts. The
rewriter is OPT-IN (the ``--with-llm`` CLI flag) and the brief always
preserves the deterministic raw text so any hallucination is auditable
against the source.

Public API::

    from cn_altdata_brief.llm import rephrase_observation, RephraseResult
    from cn_altdata_brief.llm.usage import log_usage, aggregate_usage

The Anthropic Python SDK is an OPTIONAL dependency (extras ``[llm]``).
Importing this package without the SDK installed succeeds — callers
must check ``available()`` or rely on the graceful-fallback semantics
of :func:`rephrase_observation` (which returns the raw text unchanged
when no API key / SDK is present).
"""

from __future__ import annotations

from cn_altdata_brief.llm.anthropic_client import (
    DEFAULT_LLM_MODEL,
    RephraseResult,
    RephraseStatus,
    available,
    rephrase_observation,
    validate_rephrase,
)
from cn_altdata_brief.llm.usage import aggregate_usage, log_usage

__all__ = [
    "DEFAULT_LLM_MODEL",
    "RephraseResult",
    "RephraseStatus",
    "aggregate_usage",
    "available",
    "log_usage",
    "rephrase_observation",
    "validate_rephrase",
]
