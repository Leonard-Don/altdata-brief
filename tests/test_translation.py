"""Tests for the v0.8 CN→EN translation layer.

These tests **never** make a real Anthropic API call. We inject a fake
SDK module by monkey-patching :func:`cn_altdata_brief.llm.translate._sdk_module`
(the same shim the production code uses), and we monkey-patch
``ANTHROPIC_API_KEY`` for the few tests that exercise the success path.

Coverage matrix
---------------

1. Successful translation passes validation and preserves all numbers.
2. Number drift triggers ``validation_failed`` and falls back to CN.
3. Missing API key returns CN with a banner — no exception.
4. Missing SDK returns CN with a banner — no exception.
5. Industry-name mapping is applied / validated correctly.
6. Bilingual CLI generates both ``YYYY-MM-DD.md`` and ``YYYY-MM-DD.en.md``.
7. CLI ``--languages`` parser rejects unsupported codes.
8. Token usage is tracked and forwarded to the JSONL log.
9. RSS feed produces one item per language per date.
10. Industry mapping JSON loads with the canonical entries.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from cn_altdata_brief import cli as cli_mod
from cn_altdata_brief.adapters import (
    etf_512400 as etf_mod,
)
from cn_altdata_brief.adapters import (
    index_research as ix_mod,
)
from cn_altdata_brief.adapters import (
    quant_trading as qt_mod,
)
from cn_altdata_brief.adapters import (
    super_pricing as sp_mod,
)
from cn_altdata_brief.cli import main
from cn_altdata_brief.llm import translate as translate_mod
from cn_altdata_brief.llm.translate import (
    FALLBACK_BANNER,
    TranslationResult,
    load_mapping,
    translate_brief,
    validate_translation,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


SAMPLE_CN_BRIEF = """---
date: 2026-05-17
generated_at: 2026-05-17T08:10:33Z
---

# CN AltData Brief — 2026-05-17

## 1. 政策动向

- **新能源汽车**：avg_impact=-0.388 (负向) · mentions=94 · 信号=利空
- **电网**：avg_impact=+0.100 (正向) · mentions=8 · 信号=中性

## 2. 库存信号

- **铝**：周价格变化 -1.15% · 标签=去库
- **铜**：周价格变化 -0.68% · 标签=去库

## 3. ETF 资金流

- **有色金属ETF南方** (512400) · 现价 2.207 · 涨跌 +0.36%
- NAV (2026-05-06) · 日收益 +3.85%
"""


SAMPLE_EN_BRIEF = """---
date: 2026-05-17
generated_at: 2026-05-17T08:10:33Z
---

# CN AltData Brief — 2026-05-17

## 1. Policy

- **EV / new energy vehicles**: avg_impact=-0.388 (negative) · mentions=94 · signal=bearish
- **power grid**: avg_impact=+0.100 (positive) · mentions=8 · signal=neutral

## 2. Inventory Signals

- **aluminum**: weekly price change -1.15% · tag=destocking
- **copper**: weekly price change -0.68% · tag=destocking

## 3. ETF Flow

- **ChinaAMC SSE Non-Ferrous Metals ETF** (512400) · spot price 2.207 · change +0.36%
- NAV (2026-05-06) · daily return +3.85%
"""


class _FakeMessage:
    """Minimal stand-in for an Anthropic ``Message`` response.

    The translator only reads ``.content[*].text`` and ``.usage.*_tokens``,
    so a dict-shaped fake is sufficient and avoids importing the real SDK.
    """

    def __init__(self, text: str, input_tokens: int = 1500, output_tokens: int = 1000) -> None:
        self.content = [{"text": text}]
        self.usage = {"input_tokens": input_tokens, "output_tokens": output_tokens}


class _FakeAnthropicClient:
    """Records the last create() call and returns a configured response."""

    def __init__(self, response_text: str, *, input_tokens: int = 1500, output_tokens: int = 1000) -> None:
        self._response = _FakeMessage(response_text, input_tokens, output_tokens)
        self.calls: list[dict[str, Any]] = []

        class _Messages:
            def __init__(inner_self, parent: _FakeAnthropicClient) -> None:
                inner_self._parent = parent

            def create(inner_self, **kwargs: Any) -> _FakeMessage:
                inner_self._parent.calls.append(kwargs)
                return inner_self._parent._response

        self.messages = _Messages(self)


class _FakeAnthropicSdk:
    """The ``anthropic`` module surface the translator touches."""

    def __init__(self, response_text: str, **kwargs: Any) -> None:
        self._response_text = response_text
        self._kwargs = kwargs
        self.last_client: _FakeAnthropicClient | None = None

    def Anthropic(self, **client_kwargs: Any) -> _FakeAnthropicClient:  # noqa: N802 (SDK shape)
        self.last_client = _FakeAnthropicClient(self._response_text, **self._kwargs)
        return self.last_client


@pytest.fixture
def fake_sdk_factory(monkeypatch: pytest.MonkeyPatch):
    """Install a fake anthropic SDK + API key. Returns a setter."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-real")

    def _install(response_text: str, **client_kwargs: Any) -> _FakeAnthropicSdk:
        sdk = _FakeAnthropicSdk(response_text, **client_kwargs)
        monkeypatch.setattr(translate_mod, "_sdk_module", lambda: sdk)
        return sdk

    return _install


@pytest.fixture
def patched_default_paths(
    monkeypatch: pytest.MonkeyPatch,
    super_pricing_cache: Path,
    quant_trading_cache: Path,
    index_research_tables: Path,
    etf_512400_snapshot: Path,
) -> None:
    """Same shape as the test_cli.py fixture — required for the CLI smoke test."""
    monkeypatch.setattr(sp_mod, "DEFAULT_CACHE_DIR", super_pricing_cache)
    monkeypatch.setattr(
        sp_mod,
        "DEFAULT_PUBLIC_SUMMARY",
        super_pricing_cache / "missing_public_summary.json",
    )
    monkeypatch.setattr(qt_mod, "DEFAULT_CACHE_DIR", quant_trading_cache)
    monkeypatch.setattr(
        qt_mod,
        "DEFAULT_PUBLIC_SUMMARY",
        quant_trading_cache / "missing_public_summary.json",
    )
    monkeypatch.setattr(ix_mod, "DEFAULT_TABLE_DIR", index_research_tables)
    monkeypatch.setattr(ix_mod, "DEFAULT_FIGURE_DIR", index_research_tables)
    monkeypatch.setattr(
        ix_mod,
        "DEFAULT_PUBLIC_SUMMARY",
        index_research_tables / "missing_public_summary.json",
    )
    monkeypatch.setattr(etf_mod, "DEFAULT_SNAPSHOT", etf_512400_snapshot)
    monkeypatch.setattr(etf_mod, "DEFAULT_PUBLIC_SUMMARY", etf_512400_snapshot)


# ---------------------------------------------------------------------------
# 1. Mapping JSON loads
# ---------------------------------------------------------------------------


def test_industry_mapping_loads_with_canonical_entries() -> None:
    load_mapping.cache_clear()
    mapping = load_mapping()
    assert mapping.industries["新能源汽车"].lower().startswith("ev")
    assert mapping.industries["电网"] == "power grid"
    assert mapping.commodities["铝"] == "aluminum"
    assert mapping.commodities["铜"] == "copper"
    assert mapping.instruments["有色金属ETF南方"].startswith("ChinaAMC")
    # The flat view merges all three industry-ish sections.
    flat = mapping.all_names()
    assert "新能源汽车" in flat
    assert "铜" in flat


# ---------------------------------------------------------------------------
# 2. Successful translation preserves all numbers (and bilingual CLI flow)
# ---------------------------------------------------------------------------


def test_translate_brief_success_preserves_all_numbers(
    fake_sdk_factory,
) -> None:
    sdk = fake_sdk_factory(SAMPLE_EN_BRIEF, input_tokens=1500, output_tokens=1000)
    result = translate_brief(SAMPLE_CN_BRIEF, target_language="en", model="fake-model")
    assert result.ok
    assert result.status == "ok"
    assert result.model_used == "fake-model"
    assert result.token_count == (1500, 1000)
    assert result.validation_warnings == []
    # The EN response should be the translated_md body (after frontmatter merge).
    assert "EV" in result.translated_md or "new energy vehicles" in result.translated_md
    assert "-0.388" in result.translated_md
    assert "-1.15%" in result.translated_md
    assert "+3.85%" in result.translated_md
    # The fallback banner should NOT appear on the OK path.
    assert "translation_failed_falling_back_to_source" not in result.translated_md
    # Frontmatter merge keeps the original date + adds language: en.
    assert "language: \"en\"" in result.translated_md
    assert "translation_status: \"ok\"" in result.translated_md
    # The SDK should have been called with our SYSTEM_PROMPT + glossary.
    assert sdk.last_client is not None
    create_kwargs = sdk.last_client.calls[0]
    user_msg = create_kwargs["messages"][0]["content"]
    assert "新能源汽车 → EV" in user_msg or "新能源汽车 → EV / new energy vehicles" in user_msg
    assert "铝 → aluminum" in user_msg


# ---------------------------------------------------------------------------
# 3. Number drift -> validation_failed -> fallback to CN
# ---------------------------------------------------------------------------


def test_translate_brief_number_drift_falls_back_to_cn(fake_sdk_factory) -> None:
    # Drop "-0.388" from the response — the validator should reject it.
    bad_en = SAMPLE_EN_BRIEF.replace("-0.388", "-0.4")
    fake_sdk_factory(bad_en)
    result = translate_brief(SAMPLE_CN_BRIEF, target_language="en", model="fake-model")
    assert not result.ok
    assert result.status == "validation_failed"
    # Fallback shows the CN source and the banner.
    assert FALLBACK_BANNER.strip() in result.translated_md
    assert "新能源汽车" in result.translated_md  # CN content preserved
    assert any("numbers missing" in w for w in result.validation_warnings)
    # Frontmatter records the fallback.
    assert 'translation_status: "validation_failed"' in result.translated_md


# ---------------------------------------------------------------------------
# 4. Missing API key returns CN with banner (no error)
# ---------------------------------------------------------------------------


def test_translate_brief_missing_api_key_returns_cn_with_note(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    # Pretend the SDK *is* installed so we exercise the api_key_missing branch.
    monkeypatch.setattr(translate_mod, "_sdk_module", lambda: object())
    result = translate_brief(SAMPLE_CN_BRIEF)
    assert result.status == "api_key_missing"
    assert FALLBACK_BANNER.strip() in result.translated_md
    assert any("ANTHROPIC_API_KEY" in w for w in result.validation_warnings)


def test_translate_brief_missing_sdk_returns_cn_with_note(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(translate_mod, "_sdk_module", lambda: None)
    result = translate_brief(SAMPLE_CN_BRIEF)
    assert result.status == "sdk_missing"
    assert FALLBACK_BANNER.strip() in result.translated_md
    assert any("SDK not installed" in w for w in result.validation_warnings)


# ---------------------------------------------------------------------------
# 5. Industry name mapping applied correctly
# ---------------------------------------------------------------------------


def test_validate_translation_accepts_mapped_industry_names() -> None:
    load_mapping.cache_clear()
    mapping = load_mapping()
    cn = "今日核心信号是 **新能源汽车** 的 avg_impact=-0.388。"
    en_good = "Today's core signal is EV / new energy vehicles with avg_impact=-0.388."
    ok, warnings = validate_translation(cn, en_good, mapping=mapping)
    assert ok
    assert warnings == []


def test_validate_translation_rejects_dropped_industry_name() -> None:
    load_mapping.cache_clear()
    mapping = load_mapping()
    cn = "今日核心信号是 **新能源汽车** 的 avg_impact=-0.388。"
    en_bad = "Today's core signal is X with avg_impact=-0.388."  # no EV / new energy / 新能源汽车
    ok, warnings = validate_translation(cn, en_bad, mapping=mapping)
    assert not ok
    assert any("industry name mappings" in w for w in warnings)


# ---------------------------------------------------------------------------
# 6. Bilingual CLI generates both files
# ---------------------------------------------------------------------------


def test_cli_generate_bilingual_writes_both_files(
    patched_default_paths: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Stub out the translate_brief call so the CLI test doesn't depend
    # on the SDK at all. We return a known-good translation so the
    # success path is exercised end-to-end.
    captured: dict[str, Any] = {}

    def fake_translate(brief_md: str, target_language: str = "en", **kwargs: Any) -> TranslationResult:
        captured["brief_md_len"] = len(brief_md)
        captured["target_language"] = target_language
        return TranslationResult(
            translated_md=(
                "---\n"
                "date: 2026-05-17\n"
                'language: "en"\n'
                'translation_status: "ok"\n'
                'translation_source_sha16: "deadbeefdeadbeef"\n'
                "---\n\n"
                "# CN AltData Brief — 2026-05-17 (EN)\n\n"
                "translated content placeholder.\n"
            ),
            source_hash="deadbeefdeadbeef",
            target_language=target_language,
            status="ok",
            model_used="fake-model",
            latency_ms=12.3,
            token_count=(1500, 1000),
        )

    monkeypatch.setattr(cli_mod, "translate_brief", fake_translate)

    briefs = tmp_path / "briefs"
    charts = tmp_path / "charts"
    code = main(
        [
            "generate",
            "--date",
            "2026-05-17",
            "--briefs-dir",
            str(briefs),
            "--charts-dir",
            str(charts),
            "--no-charts",
            "--no-index",
            "--no-feed",
            "--languages",
            "CN,EN",
            "--llm-usage-log",
            str(tmp_path / "usage.jsonl"),
            "--llm-model",
            "fake-model",
        ]
    )
    assert code == 0
    cn_brief = briefs / "2026-05-17.md"
    en_brief = briefs / "2026-05-17.en.md"
    assert cn_brief.exists()
    assert en_brief.exists()
    en_text = en_brief.read_text(encoding="utf-8")
    assert "language: \"en\"" in en_text
    assert "translation_status: \"ok\"" in en_text
    assert captured["target_language"] == "en"
    # Usage log should have one translation entry.
    usage_records = [
        json.loads(line)
        for line in (tmp_path / "usage.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(usage_records) == 1
    assert usage_records[0]["section"] == "translate-en"
    assert usage_records[0]["input_tokens"] == 1500
    assert usage_records[0]["output_tokens"] == 1000


# ---------------------------------------------------------------------------
# 7. CLI rejects unsupported language codes
# ---------------------------------------------------------------------------


def test_cli_generate_rejects_unsupported_language(
    patched_default_paths: None,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(
            [
                "generate",
                "--date",
                "2026-05-17",
                "--briefs-dir",
                str(tmp_path / "briefs"),
                "--charts-dir",
                str(tmp_path / "charts"),
                "--no-charts",
                "--no-index",
                "--no-feed",
                "--languages",
                "CN,XX",
            ]
        )
    assert excinfo.value.code == 2
    err = capsys.readouterr().err
    assert "unsupported language code" in err


# ---------------------------------------------------------------------------
# 8. Token cost tracked via TranslationResult helpers
# ---------------------------------------------------------------------------


def test_translation_result_exposes_token_helpers() -> None:
    result = TranslationResult(
        translated_md="...",
        source_hash="abc",
        target_language="en",
        status="ok",
        token_count=(1500, 1000),
    )
    assert result.input_tokens == 1500
    assert result.output_tokens == 1000
    assert result.ok


# ---------------------------------------------------------------------------
# 9. RSS feed produces bilingual items
# ---------------------------------------------------------------------------


def test_rss_feed_includes_bilingual_items(tmp_path: Path) -> None:
    from cn_altdata_brief.render.rss import render_feed

    briefs = tmp_path / "briefs"
    briefs.mkdir()
    (briefs / "2026-05-17.md").write_text(
        "# CN AltData Brief — 2026-05-17\n\n"
        "- **新能源汽车**：avg_impact=-0.388 (负向) · 信号=利空\n",
        encoding="utf-8",
    )
    (briefs / "2026-05-17.en.md").write_text(
        "---\nlanguage: \"en\"\ntranslation_status: \"ok\"\n---\n\n"
        "# CN AltData Brief — 2026-05-17\n\n"
        "- **EV / new energy vehicles**: avg_impact=-0.388 (negative) · signal=bearish\n",
        encoding="utf-8",
    )

    feed_path = tmp_path / "feed.xml"
    render_feed(briefs_dir=briefs, feed_path=feed_path, site_url="https://example.test")
    xml = feed_path.read_text(encoding="utf-8")
    assert "cn-altdata-brief:2026-05-17</guid>" in xml or "cn-altdata-brief:2026-05-17<" in xml
    assert "cn-altdata-brief:2026-05-17:en" in xml
    assert "[EN]" in xml
    assert "/briefs/2026-05-17.html" in xml
    assert "/briefs/2026-05-17.en.html" in xml
    # Per-item language tags are present.
    assert "<language>en</language>" in xml
    assert "<language>zh-CN</language>" in xml
