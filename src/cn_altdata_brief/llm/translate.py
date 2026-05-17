"""v0.8 — optional LLM translation layer (CN → EN).

The deterministic Chinese brief (v0.2) is the **ground truth**; this
module produces an English translation as an *additive* side-channel so
the international audience can read the same factual content without
the maintainer ever editing two parallel sources.

Design contract
---------------

* **Optional dependency** — the ``anthropic`` SDK is in the ``[llm]``
  extra. ``import`` of this module never raises even when the SDK is
  absent; ``available()`` reports the truth.
* **Env-var only** — the API key comes from ``ANTHROPIC_API_KEY``. We
  never read it from CLI args or config files.
* **Graceful fallback** — :func:`translate_brief` *never* raises. Any
  failure (missing key, network error, timeout, validation drift)
  returns a :class:`TranslationResult` whose ``translated_md`` is the
  **Chinese source** with a header banner explaining the fallback. The
  CLI / publisher handles the rest.
* **Numeric / industry-name guard** — every number that appears in the
  CN source MUST still appear in the EN output. Every industry name in
  the source MUST be replaced by its mapped English term (per
  ``industry_mapping.json``). If validation fails we fall back to CN
  with the banner — auditors and operators always know a translation
  was attempted.

Why a separate module (vs. reusing ``rephrase_observation``)?
-------------------------------------------------------------

* ``rephrase_observation`` is **Chinese → Chinese journalistic rewrite**
  with a narrow validation surface (3 sentences, single section). It
  shares prompt scaffolding but the **policies** differ:
  translation must preserve markdown structure (headings, bullets,
  tables, code fences) and must not introduce new sentences.
* The system prompt, validation policy, fallback messaging, and length
  cap are all different from the rephrase case. A separate module makes
  the contract auditable and lets tests exercise each path in
  isolation.

The two modules deliberately share helpers (``_sdk_module``,
``_safe_token_count``, ``_extract_text_from_response``) by importing
from :mod:`cn_altdata_brief.llm.anthropic_client`.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Literal

from cn_altdata_brief.llm.anthropic_client import (
    DEFAULT_LLM_MODEL,
    _extract_text_from_response,
    _safe_token_count,
    _sdk_module,
)

logger = logging.getLogger(__name__)

# Soft cap on translated length. EN is typically 1.4-1.8x CN by char
# count, so we allow 3x raw length before falling back. This catches
# runaway generations without flagging legitimate expansion.
MAX_TRANSLATION_RATIO = 3.0

# Numeric extraction — same shape as the rephrase guard but kept local
# so the translation policy can diverge without dragging rephrase tests
# along. A "number" is any decimal-or-integer token; sub-10 plain
# integers are excluded (they're usually section numerals like
# "## 1." or "5/5 OK" sub-source counts that translate stably).
_NUMBER_RE = re.compile(r"[+-]?\d+(?:\.\d+)?")

# Industry / commodity / instrument bolded names in the deterministic
# brief — used as a fallback when no `industries` list is supplied by
# the caller. The brief consistently emits ``**新能源汽车**`` style.
_BOLD_NAME_RE = re.compile(r"\*\*([^*]+)\*\*")

# Default location of the mapping file. Tests can override by passing
# an explicit ``mapping_path``.
DEFAULT_MAPPING_PATH = Path(__file__).resolve().parent / "industry_mapping.json"

# Banner inserted at the top of the EN brief when translation failed
# and we fall back to the Chinese ground-truth source. The frontmatter
# field is the machine-readable signal; the human note explains in
# English why the file is in Chinese.
FALLBACK_BANNER = (
    "> **Notice — translation_failed_falling_back_to_source.** "
    "The English translation could not be produced safely (see "
    "frontmatter `translation_status`); this file shows the Chinese "
    "ground-truth brief unchanged. Refer to the deterministic CN "
    "version for facts; an EN translation will be re-attempted on the "
    "next scheduled run.\n"
)

TranslationStatus = Literal[
    "ok",
    "sdk_missing",
    "api_key_missing",
    "api_error",
    "validation_failed",
    "too_long",
    "empty_input",
]


# ---------------------------------------------------------------------------
# Public dataclass
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class TranslationResult:
    """Outcome of one translation request.

    Attributes
    ----------
    translated_md:
        The markdown that should be written to ``YYYY-MM-DD.en.md``.
        On any non-OK status this is the **Chinese source** with a
        :data:`FALLBACK_BANNER` prepended — callers can write it
        unconditionally.
    source_hash:
        SHA-256 (first 16 hex chars) of the CN source. Useful for
        de-duplicating repeat translation requests of the same input
        and for cache lookups in future iterations.
    target_language:
        ISO-style code, e.g. ``"en"``. Echoed so callers logging the
        result don't have to remember what they requested.
    model_used:
        Anthropic model name, or ``None`` when no API call was made.
    latency_ms:
        Wall-clock latency of the API call. ``None`` when not invoked.
    token_count:
        ``(input_tokens, output_tokens)`` reported by the SDK. ``None``
        components when not invoked or when the response shape was
        unexpected.
    status:
        Coarse outcome — one of :data:`TranslationStatus`. ``"ok"``
        means the translation passed validation.
    validation_warnings:
        Human-readable strings describing any guard-rail trips. Empty
        on the OK path. Always populated on the fallback paths so logs
        / dashboards show *why* the fallback happened.
    """

    translated_md: str
    source_hash: str
    target_language: str
    status: TranslationStatus
    model_used: str | None = None
    latency_ms: float | None = None
    token_count: tuple[int | None, int | None] = (None, None)
    validation_warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    @property
    def input_tokens(self) -> int | None:
        return self.token_count[0]

    @property
    def output_tokens(self) -> int | None:
        return self.token_count[1]


# ---------------------------------------------------------------------------
# Mapping loader
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class IndustryMapping:
    """Loaded view of ``industry_mapping.json``.

    The mapping is loaded once per path via :func:`load_mapping`; tests
    can clear the cache by calling ``load_mapping.cache_clear()``.
    """

    industries: dict[str, str]
    commodities: dict[str, str]
    instruments: dict[str, str]
    section_headings: dict[str, str]
    phrases: dict[str, str]

    def all_names(self) -> dict[str, str]:
        """Flat dict of every CN→EN entry, useful for validation lookup."""
        merged: dict[str, str] = {}
        merged.update(self.industries)
        merged.update(self.commodities)
        merged.update(self.instruments)
        return merged


@lru_cache(maxsize=4)
def load_mapping(path: str | None = None) -> IndustryMapping:
    """Load the CN→EN mapping from ``industry_mapping.json``.

    Cached per path. Returns an empty :class:`IndustryMapping` if the
    file is missing or malformed (logged at WARNING) — this is
    intentional so the rest of the pipeline keeps working in a
    degraded mode.
    """
    p = Path(path) if path else DEFAULT_MAPPING_PATH
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("could not load industry mapping at %s (%s)", p, exc)
        return IndustryMapping({}, {}, {}, {}, {})
    if not isinstance(raw, dict):
        logger.warning("industry mapping at %s is not a JSON object", p)
        return IndustryMapping({}, {}, {}, {}, {})

    def _section(name: str) -> dict[str, str]:
        block = raw.get(name)
        if not isinstance(block, dict):
            return {}
        return {str(k): str(v) for k, v in block.items() if isinstance(k, str)}

    return IndustryMapping(
        industries=_section("industries"),
        commodities=_section("commodities"),
        instruments=_section("instruments"),
        section_headings=_section("section_headings"),
        phrases=_section("phrases"),
    )


# ---------------------------------------------------------------------------
# SDK / availability
# ---------------------------------------------------------------------------


def available() -> bool:
    """Return True when both the SDK and an API key are present."""
    return _sdk_module() is not None and bool(os.environ.get("ANTHROPIC_API_KEY"))


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------


SYSTEM_PROMPT = (
    "Translate this Chinese financial research brief to natural professional "
    "English. CRITICAL: preserve all numbers (percentages, currency amounts, "
    "dates) EXACTLY. Industry names use established English terms (新能源汽车 "
    "= EV/new energy vehicles). Tone: research analyst, neutral, factual. No "
    "marketing language. Preserve the markdown structure exactly — keep all "
    "headings (#, ##), bullet markers (-, *), tables, code fences, blockquotes "
    "(>) and YAML frontmatter in the same positions. Translate the prose only; "
    "leave numeric tokens, ticker symbols (e.g. 512400), filenames "
    "(*.json), URLs, ISO timestamps, and hash digests untouched. Output the "
    "translated markdown only — no commentary, no preamble, no closing notes."
)


def _build_user_prompt(
    brief_md: str,
    *,
    target_language: str,
    source_language: str,
    mapping: IndustryMapping,
    present_names: list[str],
) -> str:
    """Assemble the user-facing instruction + glossary + source.

    ``present_names`` is the subset of CN industry / commodity /
    instrument names actually appearing in ``brief_md``; we surface
    only those to the model so the glossary stays tight and audit-able.
    """
    flat = mapping.all_names()
    glossary_lines: list[str] = []
    for cn in present_names:
        en = flat.get(cn)
        if en:
            glossary_lines.append(f"- {cn} → {en}")
    # Always include a couple of high-frequency phrase mappings so the
    # model has section-heading anchors even when the bolded-name set
    # is empty (e.g. degraded "all sections missing" briefs).
    for cn, en in mapping.section_headings.items():
        glossary_lines.append(f"- {cn} → {en}")

    glossary_block = (
        "\nGLOSSARY (use exactly these English terms; do not invent synonyms):\n"
        + "\n".join(glossary_lines)
        if glossary_lines
        else "\n(No glossary entries — preserve any English terms verbatim.)"
    )

    return (
        f"Translate the following {source_language} markdown to "
        f"{target_language}. Output the translated markdown only — no "
        "preamble or postscript.\n"
        f"{glossary_block}\n\n"
        "SOURCE MARKDOWN:\n"
        "```markdown\n"
        f"{brief_md}\n"
        "```"
    )


def _source_hash(brief_md: str) -> str:
    return hashlib.sha256(brief_md.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Validation guard
# ---------------------------------------------------------------------------


def _normalize_numbers(text: str) -> set[str]:
    """Extract canonical numeric tokens (drops sub-10 plain ints)."""
    out: set[str] = set()
    for raw in _NUMBER_RE.findall(text):
        token = raw.lstrip("+")
        try:
            as_float = float(token)
        except ValueError:
            continue
        if "." not in token and abs(as_float) < 10:
            continue
        if token.endswith(".0"):
            token = token[:-2]
        out.add(token)
    return out


def _present_industry_names(brief_md: str, mapping: IndustryMapping) -> list[str]:
    """Return the CN names from the mapping that actually appear in ``brief_md``.

    We walk the bolded segments first (the deterministic synth always
    bolds industry names) and then sweep the rest of the text for any
    mapped term we might have missed. Stable order = useful glossary.

    A subtle but important rule: when a shorter mapped name is *only*
    present as a substring of a longer mapped name we already captured
    (e.g. ``汽车`` appearing only inside ``新能源汽车``, ``金`` only inside
    ``有色金属``, ``铜`` only inside ``铜钱`` if that ever existed), the
    shorter one is **not** added — otherwise the validator demands the
    English translation contain "automotive" purely because the source
    mentioned "EV / new energy vehicles", which is wrong.
    """
    flat = mapping.all_names()
    seen: list[str] = []

    def _push(name: str) -> None:
        if name and name in flat and name not in seen:
            seen.append(name)

    for m in _BOLD_NAME_RE.findall(brief_md):
        _push(m.strip())

    # Sort longer names first so when we mask occurrences below, shorter
    # names only match the *remaining* text. Single-character commodity
    # names (金, 铝, 铜, ...) are extremely ambiguous outside a bolded
    # context (`金` collides with `资金`, `黄金`, `金融`; `铜` collides
    # with `铜板`...) so we require them to have been bolded. This is
    # how the deterministic brief always emits them anyway.
    candidates = sorted(flat.keys(), key=len, reverse=True)
    masked = brief_md
    for cn in candidates:
        if cn in seen:
            # Already captured as a bolded name — mask its occurrences
            # so shorter substrings don't double-count.
            masked = masked.replace(cn, " " * len(cn))
    for cn in candidates:
        if cn in seen:
            continue
        if len(cn) < 2:
            # Single-char Chinese names are too ambiguous to discover
            # outside an explicit bolded context.
            continue
        if cn in masked:
            _push(cn)
            masked = masked.replace(cn, " " * len(cn))
    return seen


def validate_translation(
    source_md: str,
    translated_md: str,
    *,
    mapping: IndustryMapping,
) -> tuple[bool, list[str]]:
    """Return ``(ok, warnings)`` for the translation.

    Checks (in order):

    1. Length within :data:`MAX_TRANSLATION_RATIO` x source length.
    2. Every numeric token in the source survives in the translation.
    3. For every CN industry name in the source that has an English
       mapping, *either* the English term *or* the original CN token
       appears in the translation. (We tolerate keeping the CN token —
       Chinese names of foreign-listed instruments sometimes have no
       canonical English form.)
    """
    warnings: list[str] = []

    if not translated_md.strip():
        return False, ["translation is empty"]

    src_len = max(len(source_md), 1)
    if len(translated_md) > src_len * MAX_TRANSLATION_RATIO:
        return False, [
            f"translation {len(translated_md)} chars exceeds "
            f"{MAX_TRANSLATION_RATIO}x source ({src_len} chars)"
        ]

    src_numbers = _normalize_numbers(source_md)
    tgt_numbers = _normalize_numbers(translated_md)
    missing_numbers = sorted(src_numbers - tgt_numbers)
    if missing_numbers:
        return False, [f"numbers missing from translation: {missing_numbers}"]

    flat = mapping.all_names()
    present = _present_industry_names(source_md, mapping)
    translated_compact = re.sub(r"\s+", "", translated_md).lower()
    missing_industries: list[str] = []
    for cn in present:
        en = flat[cn]
        cn_compact = re.sub(r"\s+", "", cn).lower()
        # The translation may keep the CN token (rare) or use any
        # whitespace-collapsed substring of the canonical EN term. We
        # split on '/' so "EV / new energy vehicles" succeeds when the
        # model emits either side of the alias.
        candidates = [cn_compact]
        for piece in en.split("/"):
            piece_compact = re.sub(r"\s+", "", piece).lower()
            if piece_compact:
                candidates.append(piece_compact)
        if not any(c in translated_compact for c in candidates):
            missing_industries.append(f"{cn}→{en}")
    if missing_industries:
        return False, [
            f"industry name mappings not applied in translation: {missing_industries}"
        ]

    return True, warnings


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def translate_brief(
    brief_md: str,
    target_language: str = "en",
    *,
    source_language: str = "zh",
    model: str = DEFAULT_LLM_MODEL,
    timeout: float = 60.0,
    max_tokens: int = 4000,
    mapping_path: str | None = None,
) -> TranslationResult:
    """Translate ``brief_md`` and return a :class:`TranslationResult`.

    Never raises. On any failure the returned ``translated_md`` is the
    Chinese source with :data:`FALLBACK_BANNER` prepended, and
    ``status`` records the reason.
    """
    source_hash = _source_hash(brief_md)
    mapping = load_mapping(mapping_path)

    if not brief_md.strip():
        return TranslationResult(
            translated_md="",
            source_hash=source_hash,
            target_language=target_language,
            status="empty_input",
            validation_warnings=["source markdown was empty"],
        )

    sdk = _sdk_module()
    if sdk is None:
        return _fallback(
            brief_md,
            source_hash,
            target_language,
            status="sdk_missing",
            warning=(
                "anthropic SDK not installed — install with "
                "`pip install cn-altdata-brief[llm]` to enable translation."
            ),
        )

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return _fallback(
            brief_md,
            source_hash,
            target_language,
            status="api_key_missing",
            warning="ANTHROPIC_API_KEY env var is not set — translation skipped.",
        )

    present_names = _present_industry_names(brief_md, mapping)
    user_prompt = _build_user_prompt(
        brief_md,
        target_language=target_language,
        source_language=source_language,
        mapping=mapping,
        present_names=present_names,
    )

    client = sdk.Anthropic(api_key=api_key, timeout=timeout)
    started = time.monotonic()
    try:
        resp = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_prompt}],
        )
    except Exception as exc:  # network / auth / rate-limit / anything
        latency_ms = (time.monotonic() - started) * 1000
        logger.warning("Anthropic translate call failed: %s; falling back to CN", exc)
        return _fallback(
            brief_md,
            source_hash,
            target_language,
            status="api_error",
            warning=f"{type(exc).__name__}: {exc}",
            model=model,
            latency_ms=latency_ms,
        )
    latency_ms = (time.monotonic() - started) * 1000

    translated_md = _extract_text_from_response(resp).strip()
    input_tokens = _safe_token_count(resp, "input_tokens")
    output_tokens = _safe_token_count(resp, "output_tokens")

    if not translated_md:
        return _fallback(
            brief_md,
            source_hash,
            target_language,
            status="api_error",
            warning="model returned empty content",
            model=model,
            latency_ms=latency_ms,
            token_count=(input_tokens, output_tokens),
        )

    translated_md = _strip_code_fence(translated_md)

    ok, warnings = validate_translation(brief_md, translated_md, mapping=mapping)
    if not ok:
        status: TranslationStatus = (
            "too_long"
            if any("exceeds" in w for w in warnings)
            else "validation_failed"
        )
        logger.warning(
            "translation validation failed: %s; falling back to CN", warnings
        )
        return _fallback(
            brief_md,
            source_hash,
            target_language,
            status=status,
            warning="; ".join(warnings),
            model=model,
            latency_ms=latency_ms,
            token_count=(input_tokens, output_tokens),
        )

    return TranslationResult(
        translated_md=_apply_en_frontmatter(translated_md, source_hash),
        source_hash=source_hash,
        target_language=target_language,
        status="ok",
        model_used=model,
        latency_ms=latency_ms,
        token_count=(input_tokens, output_tokens),
        validation_warnings=warnings,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fallback(
    brief_md: str,
    source_hash: str,
    target_language: str,
    *,
    status: TranslationStatus,
    warning: str,
    model: str | None = None,
    latency_ms: float | None = None,
    token_count: tuple[int | None, int | None] = (None, None),
) -> TranslationResult:
    """Build a TranslationResult that ships the CN source verbatim.

    The result is always safe to write — the banner explains in EN
    why the file is still in Chinese, and the frontmatter
    ``translation_status`` field gives machines a stable signal.
    """
    fallback_md = _apply_fallback_frontmatter(brief_md, source_hash, status)
    return TranslationResult(
        translated_md=fallback_md,
        source_hash=source_hash,
        target_language=target_language,
        status=status,
        model_used=model,
        latency_ms=latency_ms,
        token_count=token_count,
        validation_warnings=[warning],
    )


def _strip_code_fence(text: str) -> str:
    """Strip a single leading/trailing ``` fence the model sometimes adds.

    Anthropic models occasionally wrap the entire response in a
    ```markdown ... ``` block even when told not to. We unwrap once;
    nested fences inside the brief are left alone.
    """
    stripped = text.strip()
    fence_match = re.match(
        r"^```(?:markdown|md)?\s*\n(.*?)\n```\s*$",
        stripped,
        flags=re.DOTALL,
    )
    if fence_match:
        return fence_match.group(1).strip()
    return stripped


def _split_frontmatter(md: str) -> tuple[str | None, str]:
    """Return (frontmatter_block_without_fences, body) or (None, md).

    The deterministic brief always emits a YAML frontmatter block —
    we lift it so the EN file can add ``translation_*`` keys while
    preserving the originals.
    """
    if not md.startswith("---\n"):
        return None, md
    end = md.find("\n---\n", 4)
    if end == -1:
        return None, md
    fm = md[4:end]
    body = md[end + 5:]
    return fm, body


def _apply_en_frontmatter(translated_md: str, source_hash: str) -> str:
    """Stamp the EN-success frontmatter onto the translated markdown.

    Adds ``translation_*`` keys to the existing YAML block. If the
    translation has its own frontmatter (the model usually preserves
    one), we splice into that block. Otherwise we wrap a fresh one.
    """
    fm, body = _split_frontmatter(translated_md)
    extra_lines = [
        'language: "en"',
        'translation_status: "ok"',
        f'translation_source_sha16: "{source_hash}"',
        'ground_truth: "../briefs/{date}.md (Chinese ground truth)"'.replace(
            "{date}", _extract_date_from_fm(fm) or "YYYY-MM-DD"
        ),
    ]
    if fm is not None:
        merged_fm = fm.rstrip() + "\n" + "\n".join(extra_lines)
        return f"---\n{merged_fm}\n---\n{body}"
    return f"---\n{chr(10).join(extra_lines)}\n---\n\n{translated_md}"


def _apply_fallback_frontmatter(
    brief_md: str, source_hash: str, status: TranslationStatus
) -> str:
    """Stamp the EN-fallback frontmatter + banner onto the CN source."""
    fm, body = _split_frontmatter(brief_md)
    extra_lines = [
        'language: "en"',
        f'translation_status: "{status}"',
        f'translation_source_sha16: "{source_hash}"',
        'ground_truth: "see Chinese version (this file is a fallback copy)"',
    ]
    if fm is not None:
        merged_fm = fm.rstrip() + "\n" + "\n".join(extra_lines)
        return f"---\n{merged_fm}\n---\n\n{FALLBACK_BANNER}\n{body.lstrip()}"
    return f"---\n{chr(10).join(extra_lines)}\n---\n\n{FALLBACK_BANNER}\n{brief_md}"


def _extract_date_from_fm(fm: str | None) -> str | None:
    if not fm:
        return None
    match = re.search(r"^date:\s*(\S+)\s*$", fm, flags=re.MULTILINE)
    return match.group(1).strip().strip('"') if match else None


# Re-export for callers that want a single import surface.
__all__ = [
    "DEFAULT_MAPPING_PATH",
    "FALLBACK_BANNER",
    "IndustryMapping",
    "TranslationResult",
    "TranslationStatus",
    "available",
    "load_mapping",
    "translate_brief",
    "validate_translation",
]
