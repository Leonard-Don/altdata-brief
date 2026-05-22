"""Anthropic Claude client for rephrasing the 本日观察 section.

Design contract
---------------

* **Optional dependency** — the ``anthropic`` SDK is in the ``[llm]``
  extra. ``import`` of this module never raises even when the SDK is
  absent; ``available()`` reports the truth.
* **Env-var only** — the API key comes from ``ANTHROPIC_API_KEY``. We
  never read it from CLI args or config files.
* **Graceful fallback** — :func:`rephrase_observation` *never* raises.
  Any failure (missing key, network error, timeout, validation drift)
  returns a :class:`RephraseResult` with ``status != "ok"`` and
  ``polished_text == raw_text``. The CLI handles the rest.
* **Numeric / industry-name guard** — every number and every industry
  name that appears in the raw deterministic text MUST still appear in
  the polished text. If the LLM drops or changes a number we use the
  raw text instead. This is the audit guarantee.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Literal

logger = logging.getLogger(__name__)

# Default Anthropic model. Kept here (not in cli.py) so docs/tests can
# import the constant without depending on argparse.
DEFAULT_LLM_MODEL = "claude-3-5-sonnet-latest"

# Per-million-token USD price assumptions for rough local estimates.
# Used only by the usage logger — the real bill is whatever Anthropic invoices.
DEFAULT_INPUT_COST_PER_MTOK = 3.0
DEFAULT_OUTPUT_COST_PER_MTOK = 15.0

# Soft cap on polished length. If the model wildly over-generates we
# prefer the raw text — the brief is meant to fit on one screen.
MAX_POLISHED_CHARS = 800

# Patterns used by the validation guard.
# A "number" in the raw text is anything looking like 12, +0.388, 3.85%
# (the policy/etf candidates use signed decimals; the inventory
# candidate uses percentages and plain integers). We strip a trailing
# ``%`` so 3.85% and 3.85 collapse to the same canonical token.
_NUMBER_RE = re.compile(r"[+-]?\d+(?:\.\d+)?")

RephraseStatus = Literal[
    "ok",
    "sdk_missing",
    "api_key_missing",
    "api_error",
    "validation_failed",
    "too_long",
]


@dataclass(slots=True)
class RephraseResult:
    """Result of one rephrase call.

    Attributes
    ----------
    polished_text:
        The text the brief should render. When ``status != "ok"`` this
        is the raw text unchanged — callers can use it directly without
        branching.
    raw_text:
        The deterministic source. Always preserved for audit / collapsible
        section in the brief.
    status:
        Coarse outcome — one of the :data:`RephraseStatus` literals.
    llm_model_used:
        Echoes the model name (or ``None`` when the LLM wasn't called).
    latency_ms:
        Wall-clock latency of the API call. ``None`` when not invoked.
    input_tokens / output_tokens:
        Token usage reported by the SDK. ``None`` when not invoked.
    prompt_hash:
        SHA-256 of the prompt text — useful for de-duplicating identical
        rephrase requests and for postmortem audit.
    note:
        Short human-readable explanation of the status (especially for
        non-OK statuses).
    """

    raw_text: str
    polished_text: str
    status: RephraseStatus
    llm_model_used: str | None = None
    latency_ms: float | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    prompt_hash: str = ""
    note: str | None = None
    industries: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status == "ok"


# ---------------------------------------------------------------------------
# SDK availability probe
# ---------------------------------------------------------------------------


def _sdk_module() -> Any | None:
    """Return the ``anthropic`` module if installed, else None.

    Kept as a function (not a module-level import) so test code can
    monkey-patch ``altdata_brief.llm.anthropic_client._sdk_module``
    to inject a fake SDK without having the real one installed.
    """
    try:
        import anthropic  # type: ignore[import-not-found]
    except ImportError:
        return None
    return anthropic


def available() -> bool:
    """Return True when both the SDK and an API key are present.

    A True result does NOT guarantee the next API call will succeed —
    it only means the prerequisites for *trying* are in place.
    """
    return _sdk_module() is not None and bool(os.environ.get("ANTHROPIC_API_KEY"))


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------


SYSTEM_PROMPT = (
    "You're rewriting a Chinese financial research brief observation "
    "section in a more journalistic style. CRITICAL: do not add facts "
    "not in the input; do not change numbers or industry names. "
    "Output: 3 sentences. Tone: 中性、新闻体、避免空泛。"
    "保留全部数字（含正负号、百分比）与行业/品种名称，按原文事实改写。"
    "不要加入价格预测、买卖建议或评论员口吻。直接输出改写后的 3 句，不要前言、不要列表、不要标题。"
)


def _build_user_prompt(raw_text: str, context: dict[str, Any]) -> str:
    """Assemble the user message.

    ``context`` is a small dict (date, industries, etc.) the synth
    layer already has on hand. It is fed to the model only as
    *guard-rails* (so the model knows which industry names must
    survive), never as new facts.
    """
    industries = context.get("industries") or []
    industries_clause = (
        "\n核心提及的行业/品种（必须原样出现在改写后的文本中）：\n- "
        + "\n- ".join(industries)
        if industries
        else ""
    )
    date_clause = f"\n数据日期：{context['date']}" if context.get("date") else ""
    return (
        "下面是 altdata-brief 当日「本日观察」段的规则化版本，"
        "请改写为 3 句新闻体中文，不增删事实：\n\n"
        f"```\n{raw_text}\n```"
        f"{date_clause}{industries_clause}"
    )


def _prompt_hash(system: str, user: str, model: str) -> str:
    h = hashlib.sha256()
    h.update(model.encode("utf-8"))
    h.update(b"\n--system--\n")
    h.update(system.encode("utf-8"))
    h.update(b"\n--user--\n")
    h.update(user.encode("utf-8"))
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Validation guard
# ---------------------------------------------------------------------------


def _normalize_numbers(text: str) -> set[str]:
    """Extract canonical numeric tokens from ``text``.

    We strip trailing ``%`` and a leading ``+`` (the deterministic
    formatter emits ``+3.85%`` while a paraphrase might say ``3.85%``).
    Plain integers below 10 are excluded — they're typically section
    numbers / persistence-day counters that drift legitimately during
    paraphrase.
    """
    matches = set()
    for raw in _NUMBER_RE.findall(text):
        token = raw.lstrip("+")
        # Drop a hanging single-digit "0" / "1" / "3" — these are usually
        # SIGNAL_PERSISTENCE_DAYS or section numerals and re-wording is fine.
        try:
            as_float = float(token)
        except ValueError:
            continue
        if "." not in token and abs(as_float) < 10:
            continue
        # Strip a trailing zero on x.0 so "0" doesn't match "0.0".
        if token.endswith(".0"):
            token = token[:-2]
        matches.add(token)
    return matches


def _extract_industries(raw_text: str, context: dict[str, Any]) -> list[str]:
    """Best-effort industry-name extraction.

    Caller (the CLI / synthesis layer) supplies an authoritative list
    via ``context['industries']`` when known. Otherwise we fall back to
    scanning the bolded segments in the raw text — the deterministic
    builders consistently emit ``**新能源汽车**`` style markup, so this
    is reliable for v0.7.
    """
    named = list(context.get("industries") or [])
    if named:
        return named
    return re.findall(r"\*\*([^*]+)\*\*", raw_text)


def validate_rephrase(
    raw_text: str, polished_text: str, industries: list[str]
) -> tuple[bool, str | None]:
    """Return ``(ok, reason)`` for the polished text.

    Checks (in order):

    1. ``polished_text`` length within :data:`MAX_POLISHED_CHARS`.
    2. Every numeric token in raw_text appears in polished_text.
    3. Every industry name in ``industries`` appears in polished_text
       (whitespace-insensitive containment).
    """
    if len(polished_text) > MAX_POLISHED_CHARS:
        return False, f"polished text {len(polished_text)} chars exceeds cap {MAX_POLISHED_CHARS}"

    raw_numbers = _normalize_numbers(raw_text)
    polished_numbers = _normalize_numbers(polished_text)
    missing_numbers = raw_numbers - polished_numbers
    if missing_numbers:
        return False, f"numbers missing from polished text: {sorted(missing_numbers)}"

    polished_compact = re.sub(r"\s+", "", polished_text)
    missing_industries = [
        name for name in industries if re.sub(r"\s+", "", name) not in polished_compact
    ]
    if missing_industries:
        return False, f"industry names missing from polished text: {missing_industries}"

    return True, None


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def rephrase_observation(
    raw_text: str,
    context: dict[str, Any] | None = None,
    *,
    model: str = DEFAULT_LLM_MODEL,
    timeout: float = 30.0,
    max_tokens: int = 600,
) -> RephraseResult:
    """Rewrite ``raw_text`` into a more journalistic register.

    Never raises. On any failure returns a result with
    ``polished_text == raw_text`` and ``status != "ok"``.
    """
    context = context or {}
    industries = _extract_industries(raw_text, context)

    sdk = _sdk_module()
    if sdk is None:
        return RephraseResult(
            raw_text=raw_text,
            polished_text=raw_text,
            status="sdk_missing",
            note="anthropic SDK not installed; run `pip install altdata-brief[llm]`",
            industries=industries,
        )

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return RephraseResult(
            raw_text=raw_text,
            polished_text=raw_text,
            status="api_key_missing",
            note="ANTHROPIC_API_KEY env var not set; using deterministic raw text",
            industries=industries,
        )

    user_prompt = _build_user_prompt(raw_text, {**context, "industries": industries})
    phash = _prompt_hash(SYSTEM_PROMPT, user_prompt, model)

    client = sdk.Anthropic(api_key=api_key, timeout=timeout)
    started = time.monotonic()
    try:
        resp = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_prompt}],
        )
    except Exception as exc:  # network, auth, rate-limit, anything
        logger.warning("Anthropic call failed: %s; falling back to raw", exc)
        latency_ms = (time.monotonic() - started) * 1000
        return RephraseResult(
            raw_text=raw_text,
            polished_text=raw_text,
            status="api_error",
            llm_model_used=model,
            latency_ms=latency_ms,
            prompt_hash=phash,
            note=f"{type(exc).__name__}: {exc}",
            industries=industries,
        )
    latency_ms = (time.monotonic() - started) * 1000

    polished_text = _extract_text_from_response(resp).strip()
    input_tokens = _safe_token_count(resp, "input_tokens")
    output_tokens = _safe_token_count(resp, "output_tokens")

    if not polished_text:
        return RephraseResult(
            raw_text=raw_text,
            polished_text=raw_text,
            status="api_error",
            llm_model_used=model,
            latency_ms=latency_ms,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            prompt_hash=phash,
            note="empty response from model",
            industries=industries,
        )

    ok, reason = validate_rephrase(raw_text, polished_text, industries)
    if not ok:
        status: RephraseStatus = (
            "too_long" if reason and reason.startswith("polished text") else "validation_failed"
        )
        logger.warning("Rephrase validation failed: %s; falling back to raw", reason)
        return RephraseResult(
            raw_text=raw_text,
            polished_text=raw_text,
            status=status,
            llm_model_used=model,
            latency_ms=latency_ms,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            prompt_hash=phash,
            note=reason,
            industries=industries,
        )

    return RephraseResult(
        raw_text=raw_text,
        polished_text=polished_text,
        status="ok",
        llm_model_used=model,
        latency_ms=latency_ms,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        prompt_hash=phash,
        industries=industries,
    )


# ---------------------------------------------------------------------------
# Response parsing helpers
# ---------------------------------------------------------------------------


def _extract_text_from_response(resp: Any) -> str:
    """Pull the text out of an Anthropic ``Message`` response.

    The real SDK returns a list of ``TextBlock`` / ``ToolUseBlock``;
    we only emit user messages so a single text block is expected. We
    accept dict-shaped responses too — that's what our tests feed in.
    """
    content = getattr(resp, "content", None)
    if content is None and isinstance(resp, dict):
        content = resp.get("content")
    if not content:
        return ""
    parts: list[str] = []
    for block in content:
        text = getattr(block, "text", None)
        if text is None and isinstance(block, dict):
            text = block.get("text")
        if text:
            parts.append(text)
    return "".join(parts)


def _safe_token_count(resp: Any, attr: str) -> int | None:
    usage = getattr(resp, "usage", None)
    if usage is None and isinstance(resp, dict):
        usage = resp.get("usage")
    if usage is None:
        return None
    value = getattr(usage, attr, None)
    if value is None and isinstance(usage, dict):
        value = usage.get(attr)
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None
