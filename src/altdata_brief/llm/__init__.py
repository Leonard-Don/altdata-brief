"""v0.7–v0.8 — optional LLM side-channels (rephrase + translate).

The deterministic synthesis pipeline (v0.2) is the *ground truth*; this
package adds two opt-in side-channels:

* **v0.7 rephrase** — rewrites the rule-based ``本日观察`` Chinese prose
  into a more journalistic Chinese register without changing any
  facts. CLI flag: ``--with-llm``.
* **v0.8 translate** — produces an English (or other-language) version
  of the full brief from the Chinese ground truth. CLI flag:
  ``--languages CN,EN``.

Both layers preserve the deterministic source so any hallucination is
auditable against the upstream cache.

Public API::

    from altdata_brief.llm import rephrase_observation, RephraseResult
    from altdata_brief.llm import translate_brief, TranslationResult
    from altdata_brief.llm.usage import log_usage, aggregate_usage

The Anthropic Python SDK is an OPTIONAL dependency (extras ``[llm]``).
Importing this package without the SDK installed succeeds — callers
must check ``available()`` or rely on the graceful-fallback semantics
of :func:`rephrase_observation` / :func:`translate_brief` (which both
return the raw / source text unchanged when no API key / SDK is
present).
"""

from __future__ import annotations

from altdata_brief.llm.anthropic_client import (
    DEFAULT_LLM_MODEL,
    RephraseResult,
    RephraseStatus,
    available,
    rephrase_observation,
    validate_rephrase,
)
from altdata_brief.llm.translate import (
    FALLBACK_BANNER,
    IndustryMapping,
    TranslationResult,
    TranslationStatus,
    load_mapping,
    translate_brief,
    validate_translation,
)
from altdata_brief.llm.usage import aggregate_usage, log_usage

__all__ = [
    "DEFAULT_LLM_MODEL",
    "FALLBACK_BANNER",
    "IndustryMapping",
    "RephraseResult",
    "RephraseStatus",
    "TranslationResult",
    "TranslationStatus",
    "aggregate_usage",
    "available",
    "load_mapping",
    "log_usage",
    "rephrase_observation",
    "translate_brief",
    "validate_rephrase",
    "validate_translation",
]
