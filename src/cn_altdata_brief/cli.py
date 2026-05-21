"""``cn-altdata-brief`` — single CLI entrypoint.

Usage::

    cn-altdata-brief generate              # generate today's brief
    cn-altdata-brief generate --date 2026-05-17
    cn-altdata-brief generate --output-dir /tmp/briefs
    cn-altdata-brief validate              # run data-quality preconditions
    cn-altdata-brief validate --fail-on-warn  # CI mode
    cn-altdata-brief publish               # push today's brief to gh-pages (v0.6)
    cn-altdata-brief publish --dry-run     # show what would happen
    cn-altdata-brief publish --date 2026-05-17 --gh-pages-branch site
    cn-altdata-brief weekly-digest         # (v0.9) aggregate this week's briefs
    cn-altdata-brief weekly-digest --week-of 2026-05-17
    cn-altdata-brief weekly-digest --with-llm  # also emit EN translation
    cn-altdata-brief monthly-digest        # (v0.11) aggregate last month
    cn-altdata-brief monthly-digest --month-of 2026-04
    cn-altdata-brief monthly-digest --with-llm  # also emit EN translation
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from cn_altdata_brief import __version__
from cn_altdata_brief.adapters import build_default_adapters
from cn_altdata_brief.adapters.base import AdapterError, AdapterPayload
from cn_altdata_brief.config import (
    load_source_config,
    source_mode_to_kwargs,
)
from cn_altdata_brief.digest import (
    collect_brief_paths_for_month,
    collect_brief_paths_for_week,
    collect_digest_paths_for_month,
    compose_monthly_digest,
    compose_weekly_digest,
    iso_week_bounds,
    month_bounds,
    previous_month,
)
from cn_altdata_brief.llm import (
    DEFAULT_LLM_MODEL,
    aggregate_usage,
    log_usage,
    rephrase_observation,
    translate_brief,
)
from cn_altdata_brief.llm.translate import TranslationResult
from cn_altdata_brief.publish import GhPagesPublisher
from cn_altdata_brief.publish.gh_pages import (
    PublishError,
    default_template_dir,
)
from cn_altdata_brief.publish.og_metadata import (
    DEFAULT_SITE_URL,
    generate_og_tags,
)
from cn_altdata_brief.render import (
    render_all_charts,
    render_brief_markdown,
    render_monthly_digest_markdown,
    render_site_index,
    render_weekly_digest_markdown,
)
from cn_altdata_brief.render.rss import render_atom_feed, render_feed
from cn_altdata_brief.synthesis import (
    synthesize_etf_flow,
    synthesize_industry,
    synthesize_inventory,
    synthesize_observation,
    synthesize_policy,
)
from cn_altdata_brief.validate import (
    EXIT_FAIL,
    EXIT_OK,
    EXIT_WARN,
    resolve_all_sources,
    run_all_checks,
    summarize,
)
from cn_altdata_brief.validate_quality import run_strict_checks

SOURCE_MODE_CHOICES = ("auto", "public", "cache", "live")
# v0.8: bilingual support. ``CN`` is the deterministic ground truth and
# always runs; ``EN`` is an LLM translation produced from the CN file
# after it has been written. Additional ISO-style codes can be added
# later (e.g. ``JP``) by extending ``_LANG_FILE_SUFFIX``.
SUPPORTED_LANGUAGES = ("CN", "EN")
_LANG_FILE_SUFFIX = {
    "CN": "",       # canonical filename, e.g. 2026-05-17.md
    "EN": ".en",    # 2026-05-17.en.md — sibling of the CN file
}

PROJECT_ROOT = Path(__file__).resolve().parents[2]  # src/cn_altdata_brief/cli.py -> project root
DEFAULT_CADENCE_TZ = ZoneInfo("Asia/Shanghai")
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "output"
DEFAULT_BRIEFS_DIR = DEFAULT_OUTPUT_DIR / "briefs"
DEFAULT_CHARTS_DIR = DEFAULT_OUTPUT_DIR / "charts"
DEFAULT_FEED_PATH = DEFAULT_OUTPUT_DIR / "feed.xml"
DEFAULT_ATOM_PATH = DEFAULT_OUTPUT_DIR / "feed.atom"
DEFAULT_LLM_USAGE_LOG = DEFAULT_OUTPUT_DIR / "llm_usage.jsonl"
DEFAULT_GH_PAGES_BRANCH = "gh-pages"
DEFAULT_DIGESTS_DIR = DEFAULT_OUTPUT_DIR / "digests"

logger = logging.getLogger(__name__)


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    )

    if args.command == "generate":
        return _cmd_generate(args)
    if args.command == "validate":
        return _cmd_validate(args)
    if args.command == "publish":
        return _cmd_publish(args)
    if args.command == "llm-usage":
        return _cmd_llm_usage(args)
    if args.command == "weekly-digest":
        return _cmd_weekly_digest(args)
    if args.command == "monthly-digest":
        return _cmd_monthly_digest(args)

    parser.print_help()
    return 1


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cn-altdata-brief",
        description="Daily alt-data research brief generator.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose logging.")
    subparsers = parser.add_subparsers(dest="command", required=False)

    gen = subparsers.add_parser("generate", help="Generate today's brief.")
    gen.add_argument(
        "--date",
        default=None,
        help="Date stamp YYYY-MM-DD (default: today UTC).",
    )
    gen.add_argument(
        "--briefs-dir",
        default=str(DEFAULT_BRIEFS_DIR),
        help=f"Where to write the brief markdown (default: {DEFAULT_BRIEFS_DIR}).",
    )
    gen.add_argument(
        "--charts-dir",
        default=str(DEFAULT_CHARTS_DIR),
        help=f"Where to write chart PNGs (default: {DEFAULT_CHARTS_DIR}).",
    )
    gen.add_argument(
        "--no-charts",
        action="store_true",
        help="Skip matplotlib chart generation (faster for headless smoke tests).",
    )
    gen.add_argument(
        "--no-index",
        action="store_true",
        help="Skip writing the briefs index.md (useful in tests).",
    )
    gen.add_argument(
        "--no-feed",
        action="store_true",
        help="Skip writing the RSS feed.xml (useful in tests / CI smoke).",
    )
    gen.add_argument(
        "--site-url",
        default=DEFAULT_SITE_URL,
        help=f"Base URL used for RSS/Atom and share metadata (default: {DEFAULT_SITE_URL}).",
    )
    gen.add_argument(
        "--source-mode",
        choices=SOURCE_MODE_CHOICES,
        default="auto",
        help=(
            "Where to pull each adapter's data. "
            "'auto' = live → public summary → cache (default). "
            "'public' = public summary only (CI mode; fails fast if missing). "
            "'cache' = bypass public summary, read internal caches. "
            "'live' = same as auto but force-enables HTTP live endpoints."
        ),
    )
    gen.add_argument(
        "--with-llm",
        action="store_true",
        help=(
            "Opt in to LLM rephrasing for the '本日观察' section only. "
            "Failures or validation drift fall back to deterministic text."
        ),
    )
    gen.add_argument(
        "--llm-model",
        default=DEFAULT_LLM_MODEL,
        help=f"Anthropic model used with --with-llm (default: {DEFAULT_LLM_MODEL}).",
    )
    gen.add_argument(
        "--llm-usage-log",
        default=str(DEFAULT_LLM_USAGE_LOG),
        help=f"Append-only JSONL usage log for --with-llm (default: {DEFAULT_LLM_USAGE_LOG}).",
    )
    gen.add_argument(
        "--languages",
        default="CN",
        help=(
            "Comma-separated list of languages to produce. CN is always "
            "the deterministic ground truth; EN (and future codes) are "
            "LLM translations produced from the CN file. Implies "
            "--with-llm for non-CN languages. Example: --languages CN,EN. "
            f"Supported: {','.join(SUPPORTED_LANGUAGES)}."
        ),
    )
    gen.add_argument(
        "-v", "--verbose", action="store_true", help="Verbose logging (subcommand-scoped alias)."
    )

    val = subparsers.add_parser(
        "validate", help="Run data-quality preconditions before publishing."
    )
    val.add_argument(
        "--fail-on-warn",
        action="store_true",
        help="Treat WARN-level issues as failures (exit 2). Use in CI.",
    )
    val.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON instead of the human summary.",
    )
    val.add_argument(
        "--source-mode",
        choices=SOURCE_MODE_CHOICES,
        default="auto",
        help=(
            "Pass-through to the underlying adapter resolution; see "
            "`generate --source-mode --help`."
        ),
    )
    val.add_argument(
        "--strict",
        action="store_true",
        help=(
            "Also run the v0.12 content-quality checks: fingerprint freshness, "
            "signal density, cross-source consistency, schema regression, "
            "placeholder detector (ERROR-level), temporal coherence, and "
            "required upstream path audit. "
            "Equivalent to --check-fingerprint --check-density --check-consistency "
            "--check-schema --check-placeholder --check-temporal "
            "--check-required-paths. Default validate keeps backward-compat "
            "with v0.2."
        ),
    )
    val.add_argument(
        "--check-fingerprint",
        action="store_true",
        help="Run only the content_fingerprint_freshness check (subset of --strict).",
    )
    val.add_argument(
        "--check-density",
        action="store_true",
        help="Run only the signal_density check (subset of --strict).",
    )
    val.add_argument(
        "--check-consistency",
        action="store_true",
        help="Run only the cross_source_consistency check (subset of --strict).",
    )
    val.add_argument(
        "--check-schema",
        action="store_true",
        help="Run only the schema_regression check (subset of --strict).",
    )
    val.add_argument(
        "--check-placeholder",
        action="store_true",
        help=(
            "Run only the placeholder_detector check (subset of --strict). "
            "FAILs the pipeline when a payload string matches a known "
            "placeholder pattern (e.g. '测试', 'TODO', 'placeholder')."
        ),
    )
    val.add_argument(
        "--check-temporal",
        action="store_true",
        help=(
            "Run only the temporal_coherence check (subset of --strict). "
            "Warns when day-over-day signal flips exceed the threshold "
            "without a declared regime_change_event."
        ),
    )
    val.add_argument(
        "--check-required-paths",
        action="store_true",
        help=(
            "Run only the required_paths check (subset of --strict). "
            "Audits raw public-summary JSON for nested upstream paths that "
            "adapters require before normalizing."
        ),
    )
    val.add_argument(
        "-v", "--verbose", action="store_true", help="Verbose logging (subcommand-scoped alias)."
    )

    pub = subparsers.add_parser(
        "publish",
        help="(v0.6) Publish today's brief to the gh-pages branch.",
    )
    pub.add_argument(
        "--date",
        default=None,
        help="Date stamp YYYY-MM-DD (default: today UTC).",
    )
    pub.add_argument(
        "--briefs-dir",
        default=str(DEFAULT_BRIEFS_DIR),
        help=f"Source briefs directory (default: {DEFAULT_BRIEFS_DIR}).",
    )
    pub.add_argument(
        "--charts-dir",
        default=str(DEFAULT_CHARTS_DIR),
        help=f"Source charts directory (default: {DEFAULT_CHARTS_DIR}).",
    )
    pub.add_argument(
        "--feed-path",
        default=str(DEFAULT_FEED_PATH),
        help=f"Optional RSS feed to publish (default: {DEFAULT_FEED_PATH}).",
    )
    pub.add_argument(
        "--atom-path",
        default=str(DEFAULT_ATOM_PATH),
        help=(
            "v0.10 — optional Atom 1.0 feed to publish alongside the RSS feed "
            f"(default: {DEFAULT_ATOM_PATH})."
        ),
    )
    pub.add_argument(
        "--digests-dir",
        default=str(DEFAULT_DIGESTS_DIR),
        help=(
            "v0.9 — weekly digests directory. Every digest md file inside "
            "is shipped to the gh-pages branch and indexed on the landing "
            f"page (default: {DEFAULT_DIGESTS_DIR})."
        ),
    )
    pub.add_argument(
        "--gh-pages-branch",
        default=DEFAULT_GH_PAGES_BRANCH,
        help=f"Branch to publish to (default: {DEFAULT_GH_PAGES_BRANCH}).",
    )
    pub.add_argument(
        "--repo-root",
        default=str(PROJECT_ROOT),
        help="Git repo root (default: project root).",
    )
    pub.add_argument(
        "--template-dir",
        default=str(default_template_dir()),
        help="Jekyll template overlay dir (default: <repo>/gh-pages-template).",
    )
    pub.add_argument(
        "--dry-run",
        action="store_true",
        help="Plan only — show the file list and exit without touching git.",
    )
    pub.add_argument(
        "--no-push",
        action="store_true",
        help="Commit to gh-pages locally but don't push to origin.",
    )
    pub.add_argument(
        "-v", "--verbose", action="store_true", help="Verbose logging (subcommand-scoped alias)."
    )

    digest = subparsers.add_parser(
        "weekly-digest",
        help="(v0.9) Aggregate the past week's daily briefs into one digest.",
    )
    digest.add_argument(
        "--week-of",
        default=None,
        help=(
            "Anchor date (YYYY-MM-DD) inside the target week. Defaults to "
            "today's UTC date; the week starts on Monday and ends Friday."
        ),
    )
    digest.add_argument(
        "--briefs-dir",
        default=str(DEFAULT_BRIEFS_DIR),
        help=f"Daily briefs source directory (default: {DEFAULT_BRIEFS_DIR}).",
    )
    digest.add_argument(
        "--digests-dir",
        default=str(DEFAULT_DIGESTS_DIR),
        help=f"Digest output directory (default: {DEFAULT_DIGESTS_DIR}).",
    )
    digest.add_argument(
        "--output",
        default=None,
        help=(
            "Explicit output path. When omitted, the digest is written to "
            "<digests-dir>/<iso_year>-W<week>.md."
        ),
    )
    digest.add_argument(
        "--recurrence-threshold",
        type=int,
        default=3,
        help=(
            "Minimum number of distinct days an industry / metal must "
            "appear before it qualifies as a theme (default: 3)."
        ),
    )
    digest.add_argument(
        "--with-llm",
        action="store_true",
        help=(
            "Also emit an English sibling (<base>.en.md) using the v0.8 "
            "translator. Falls back to CN with a banner on translation "
            "failure — same contract as the daily brief."
        ),
    )
    digest.add_argument(
        "--llm-model",
        default=DEFAULT_LLM_MODEL,
        help=f"Anthropic model used with --with-llm (default: {DEFAULT_LLM_MODEL}).",
    )
    digest.add_argument(
        "--llm-usage-log",
        default=str(DEFAULT_LLM_USAGE_LOG),
        help=f"Append-only JSONL usage log for --with-llm (default: {DEFAULT_LLM_USAGE_LOG}).",
    )
    digest.add_argument(
        "-v", "--verbose", action="store_true", help="Verbose logging (subcommand-scoped alias)."
    )

    monthly = subparsers.add_parser(
        "monthly-digest",
        help="(v0.11) Aggregate the past month's daily briefs + weekly digests into one monthly digest.",
    )
    monthly.add_argument(
        "--month-of",
        default=None,
        help=(
            "Month to aggregate, either ``YYYY-MM`` (e.g. 2026-04) or a "
            "YYYY-MM-DD date inside the target month. Defaults to LAST "
            "month — the typical 'first business day of next month' run."
        ),
    )
    monthly.add_argument(
        "--briefs-dir",
        default=str(DEFAULT_BRIEFS_DIR),
        help=f"Daily briefs source directory (default: {DEFAULT_BRIEFS_DIR}).",
    )
    monthly.add_argument(
        "--digests-dir",
        default=str(DEFAULT_DIGESTS_DIR),
        help=(
            "Weekly digests directory (also where the monthly digest is "
            f"written by default; default: {DEFAULT_DIGESTS_DIR})."
        ),
    )
    monthly.add_argument(
        "--output",
        default=None,
        help=(
            "Explicit output path. When omitted, the digest is written to "
            "<digests-dir>/<YYYY-MM>.md."
        ),
    )
    monthly.add_argument(
        "--sustained-threshold",
        type=int,
        default=12,
        help=(
            "Minimum number of distinct days a name must appear before "
            "it qualifies as a sustained monthly theme (default: 12)."
        ),
    )
    monthly.add_argument(
        "--with-llm",
        action="store_true",
        help=(
            "Also emit an English sibling (<base>.en.md) via the v0.8 "
            "translator. Falls back to CN with a banner on failure."
        ),
    )
    monthly.add_argument(
        "--llm-model",
        default=DEFAULT_LLM_MODEL,
        help=f"Anthropic model used with --with-llm (default: {DEFAULT_LLM_MODEL}).",
    )
    monthly.add_argument(
        "--llm-usage-log",
        default=str(DEFAULT_LLM_USAGE_LOG),
        help=f"Append-only JSONL usage log for --with-llm (default: {DEFAULT_LLM_USAGE_LOG}).",
    )
    monthly.add_argument(
        "-v", "--verbose", action="store_true", help="Verbose logging (subcommand-scoped alias)."
    )

    usage = subparsers.add_parser(
        "llm-usage",
        help="Summarize the optional LLM usage log.",
    )
    usage.add_argument(
        "--usage-log",
        default=str(DEFAULT_LLM_USAGE_LOG),
        help=f"Path to llm_usage.jsonl (default: {DEFAULT_LLM_USAGE_LOG}).",
    )
    usage.add_argument(
        "--days",
        type=int,
        default=None,
        help="Only include records from the last N days (default: lifetime).",
    )
    usage.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON.",
    )
    usage.add_argument(
        "-v", "--verbose", action="store_true", help="Verbose logging (subcommand-scoped alias)."
    )

    return parser


# ---------------------------------------------------------------------------


def _parse_generate_date(raw_date: str | None) -> str:
    if raw_date is None:
        return _today_cadence_date()

    try:
        parsed = datetime.strptime(raw_date, "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError(f"invalid --date {raw_date!r}; expected YYYY-MM-DD") from exc

    if parsed.isoformat() != raw_date:
        raise ValueError(f"invalid --date {raw_date!r}; expected YYYY-MM-DD")
    return raw_date


def _today_cadence_date() -> str:
    """Return the publishing cadence date in Beijing time."""
    return datetime.now(DEFAULT_CADENCE_TZ).strftime("%Y-%m-%d")


def _cmd_generate(args: argparse.Namespace) -> int:
    try:
        date = _parse_generate_date(args.date)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    try:
        languages = _parse_languages(args)
    except SystemExit as exc:
        return int(exc.code or 1)

    briefs_dir = Path(args.briefs_dir)
    charts_root = Path(args.charts_dir) / date
    briefs_dir.mkdir(parents=True, exist_ok=True)

    cfg = load_source_config(**source_mode_to_kwargs(args.source_mode))
    adapters = build_default_adapters(config=cfg)
    payloads: dict[str, AdapterPayload | None] = {}
    public_required_missing: list[str] = []
    for name, adapter in adapters.items():
        try:
            payloads[name] = adapter.fetch()
            mode = payloads[name].data.get("source_mode", "?") if payloads[name] else "?"
            logger.info(
                "adapter %s ok (mode=%s, live=%s)",
                name,
                mode,
                payloads[name].live,
            )
        except AdapterError as exc:
            logger.warning("adapter %s unavailable: %s", name, exc)
            payloads[name] = None
            if cfg.public_required:
                public_required_missing.append(name)

    if args.source_mode == "public" and public_required_missing:
        print(
            "ERROR: --source-mode=public requires public summaries for "
            f"{public_required_missing}; rerun with --source-mode=auto for "
            "local use, or commit/copy the public summary JSON into place.",
            file=sys.stderr,
        )
        return 2

    if all(p is None for p in payloads.values()):
        print("ERROR: every adapter failed; cannot generate brief.", file=sys.stderr)
        return 2

    sections = _synthesize(payloads)
    llm_context = _maybe_rephrase_observation(sections["observation"], date=date, args=args)

    if args.no_charts:
        chart_paths: dict[str, Path] = {}
    else:
        chart_paths = render_all_charts(
            output_dir=charts_root,
            policy_top=sections["policy"].get("top_industries"),
            metals=sections["inventory"].get("metals"),
            industry_top=sections["industry"].get("top_industries"),
            nav_trend=(payloads["etf_512400"].data.get("recent_nav") if payloads.get("etf_512400") else None),
        )

    rel_charts = {k: _relative_to(briefs_dir, v) for k, v in chart_paths.items()}

    context = {
        "date": date,
        "fetched_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "policy": sections["policy"],
        "inventory": sections["inventory"],
        "etf_flow": sections["etf_flow"],
        "industry": sections["industry"],
        "observation": sections["observation"],
        "charts": rel_charts,
        "llm": llm_context,
    }
    markdown = render_brief_markdown(context=context)
    brief_path = briefs_dir / f"{date}.md"
    brief_path.write_text(markdown, encoding="utf-8")

    # v0.5: maintain a stable `latest.md` symlink so external readers
    # (e.g. launchd cron jobs, RSS regenerators, publish branches) have
    # a fixed filename to point at without knowing today's date stamp.
    _refresh_latest_symlink(briefs_dir, brief_path)

    # v0.8: optional bilingual translation. CN is always written above;
    # additional languages are produced from the CN markdown. We pass
    # the deterministic CN file (not in-memory string) so the translation
    # input is reproducible by hand from disk.
    translation_paths: list[Path] = []
    translation_results: dict[str, TranslationResult] = {}
    for lang in languages:
        if lang == "CN":
            continue
        result, lang_path = _produce_translation(
            brief_path=brief_path,
            language=lang,
            date=date,
            args=args,
        )
        translation_results[lang] = result
        translation_paths.append(lang_path)

    if not args.no_index:
        render_site_index(briefs_dir)

    feed_path: Path | None = None
    if not args.no_feed:
        feed_path = briefs_dir.parent / "feed.xml"
        digests_dir = briefs_dir.parent / "digests"
        # v0.10 — feed both RSS 2.0 and Atom 1.0 with OG enrichment.
        sections_by_date = {date: sections}
        chart_root = Path(args.charts_dir)
        render_feed(
            briefs_dir=briefs_dir,
            feed_path=feed_path,
            site_url=args.site_url,
            digests_dir=digests_dir if digests_dir.exists() else None,
            chart_dir=chart_root,
            sections_by_date=sections_by_date,
        )
        atom_path = briefs_dir.parent / "feed.atom"
        render_atom_feed(
            briefs_dir=briefs_dir,
            feed_path=atom_path,
            site_url=args.site_url,
            digests_dir=digests_dir if digests_dir.exists() else None,
            chart_dir=chart_root,
            sections_by_date=sections_by_date,
        )

    # v0.10 — emit OG frontmatter so the Jekyll layout picks up the
    # right per-brief metadata. The publisher inlines this via Liquid
    # ``page.og_*`` lookups (see _layouts/brief.html). We rewrite the
    # CN brief's frontmatter rather than producing a sidecar so a
    # single ``YYYY-MM-DD.md`` is the canonical artifact.
    site_url = getattr(args, "site_url", None) or DEFAULT_SITE_URL
    og_tags = generate_og_tags(
        brief_path,
        site_url=site_url,
        sections=sections,
        chart_dir=charts_root,
    )
    _inject_og_frontmatter(brief_path, og_tags)
    for lang_path in translation_paths:
        # Translation files share the same OG image / structure; only
        # the locale changes. Keep the title/description in CN for now
        # — translating them adds another LLM call we don't want on a
        # critical-path commit.
        en_tags = dict(og_tags)
        en_tags["og:locale"] = "en_US"
        _inject_og_frontmatter(lang_path, en_tags)

    available_count = sum(1 for s in sections.values() if s.get("available"))
    feed_suffix = f" · feed={feed_path.name}" if feed_path else ""
    mode_summary = ",".join(
        f"{name}={(p.data.get('source_mode', '?') if p else 'missing')}"
        for name, p in payloads.items()
    )
    lang_suffix = ""
    if translation_paths:
        lang_tags = []
        for lang in languages:
            if lang == "CN":
                continue
            r = translation_results.get(lang)
            status = r.status if r else "missing"
            lang_tags.append(f"{lang}={status}")
        lang_suffix = (
            f" · langs={','.join(languages)} · translations=[{', '.join(lang_tags)}]"
        )
    print(
        f"OK · wrote {brief_path} · {available_count}/5 sections available · "
        f"{len(chart_paths)} charts{feed_suffix}{lang_suffix} · mode={args.source_mode} · "
        f"resolved=[{mode_summary}] · sources: "
        + ", ".join(sorted(p.source for p in payloads.values() if p is not None))
    )
    return 0


def _parse_languages(args: argparse.Namespace) -> list[str]:
    """Parse ``--languages`` and return a de-duplicated ordered list.

    CN is always present (the deterministic ground truth is required).
    Unknown codes raise ``SystemExit`` via the argparse-style error
    surface so the CLI fails loudly rather than silently dropping
    languages the user asked for.
    """
    raw = getattr(args, "languages", "CN") or "CN"
    requested: list[str] = []
    for token in raw.split(","):
        code = token.strip().upper()
        if not code:
            continue
        if code not in SUPPORTED_LANGUAGES:
            print(
                f"ERROR: unsupported language code {code!r}; "
                f"supported: {','.join(SUPPORTED_LANGUAGES)}",
                file=sys.stderr,
            )
            raise SystemExit(2)
        if code not in requested:
            requested.append(code)
    if "CN" not in requested:
        requested.insert(0, "CN")
    return requested


def _produce_translation(
    *,
    brief_path: Path,
    language: str,
    date: str,
    args: argparse.Namespace,
) -> tuple[TranslationResult, Path]:
    """Translate ``brief_path`` to ``language`` and write the result.

    Always writes a file at the target language path — even on
    fallback the file is valuable (it carries the banner explaining
    the fallback to readers who only follow the EN URL).
    Logs one entry to ``llm_usage.jsonl`` per call.
    """
    source_md = brief_path.read_text(encoding="utf-8")
    target_iso = language.lower()
    model = str(getattr(args, "llm_model", DEFAULT_LLM_MODEL))
    result = translate_brief(
        source_md,
        target_language=target_iso,
        model=model,
    )
    suffix = _LANG_FILE_SUFFIX.get(language, f".{target_iso}")
    lang_path = brief_path.parent / f"{date}{suffix}.md"
    lang_path.write_text(result.translated_md, encoding="utf-8")
    _log_translation_usage(result, args=args, date=date, language=language)
    return result, lang_path


def _log_translation_usage(
    result: TranslationResult,
    *,
    args: argparse.Namespace,
    date: str,
    language: str,
) -> None:
    """Shim that funnels a TranslationResult through the rephrase log_usage.

    The on-disk log format already supports the fields we care about
    (model, status, latency, tokens, cost) — we adapt our dataclass to
    the shape :func:`log_usage` expects so all LLM activity lands in
    one append-only JSONL.
    """
    from cn_altdata_brief.llm.anthropic_client import RephraseResult

    adapted = RephraseResult(
        raw_text="",  # we deliberately never persist the source markdown
        polished_text="",  # ditto the translation
        status=result.status if result.status != "empty_input" else "validation_failed",
        llm_model_used=result.model_used,
        latency_ms=result.latency_ms,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        prompt_hash=result.source_hash,
    )
    log_usage(
        adapted,
        Path(getattr(args, "llm_usage_log", DEFAULT_LLM_USAGE_LOG)),
        extra={"date": date, "section": f"translate-{language.lower()}"},
    )


def _strict_includes_from_args(args: argparse.Namespace) -> tuple[str, ...] | None:
    """Translate validate CLI flags into a tuple of strict-check identifiers.

    Returns ``None`` when no quality checks were requested (default
    validate run preserves v0.2 surface). Returns the full tuple when
    ``--strict`` is set; otherwise returns the subset selected by the
    per-check flags.
    """
    if getattr(args, "strict", False):
        return (
            "fingerprint",
            "density",
            "consistency",
            "schema",
            "placeholder",
            "temporal",
            "required_paths",
        )
    subset: list[str] = []
    for flag, key in (
        ("check_fingerprint", "fingerprint"),
        ("check_density", "density"),
        ("check_consistency", "consistency"),
        ("check_schema", "schema"),
        ("check_placeholder", "placeholder"),
        ("check_temporal", "temporal"),
        ("check_required_paths", "required_paths"),
    ):
        if getattr(args, flag, False):
            subset.append(key)
    return tuple(subset) if subset else None


def _cmd_validate(args: argparse.Namespace) -> int:
    source_mode = getattr(args, "source_mode", "auto")
    cfg = load_source_config(**source_mode_to_kwargs(source_mode))
    adapters = build_default_adapters(config=cfg)
    # v0.4: probe per-adapter resolution BEFORE fetching, so the report
    # still shows "what would have been used" even when fetch raises.
    resolutions = resolve_all_sources(adapters)
    payloads: dict[str, AdapterPayload | None] = {}
    for name, adapter in adapters.items():
        try:
            payloads[name] = adapter.fetch()
        except AdapterError as exc:
            logger.warning("validate: adapter %s unavailable: %s", name, exc)
            payloads[name] = None

    results = run_all_checks(
        payloads,
        allow_missing_cache_only_sources=(source_mode == "public"),
    )

    # v0.12: opt-in content-quality checks. --strict turns them all on;
    # individual --check-* flags select a subset. The default validate
    # call (no flags) preserves v0.2 backward-compatible behavior.
    strict_includes = _strict_includes_from_args(args)
    if strict_includes is not None:
        results.extend(run_strict_checks(payloads, include=strict_includes))

    code = summarize(results, fail_on_warn=args.fail_on_warn)

    if args.json:
        out = {
            "checks": [
                {
                    "name": r.name,
                    "level": r.level,
                    "message": r.message,
                    "detail": r.detail,
                }
                for r in results
            ],
            "resolutions": {
                name: res.to_dict() for name, res in resolutions.items()
            },
            "exit_code": code,
            "fail_on_warn": bool(args.fail_on_warn),
        }
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        for r in results:
            print(r.to_line())
        # Per-adapter source-resolution section — useful for debugging
        # "why did this adapter pick cache when I wanted public?".
        print("--- per-adapter source resolution ---")
        for name, res in resolutions.items():
            status = "OK  " if res.available else "MISS"
            tail = f" · mtime={res.mtime_iso}" if res.mtime_iso else ""
            note = f" · {res.note}" if res.note else ""
            print(
                f"[{status}] {name}: mode={res.mode}"
                f"{' · path=' + str(res.path) if res.path else ''}"
                f"{tail}{note}"
            )
        verdict = {EXIT_OK: "ALL CHECKS PASSED", EXIT_WARN: "WARNINGS", EXIT_FAIL: "FAILURES"}[code]
        print(f"--- {verdict} (exit={code}) ---")
    return code


def _cmd_llm_usage(args: argparse.Namespace) -> int:
    aggregate = aggregate_usage(Path(args.usage_log), days=args.days)
    out = {
        "days": aggregate.days,
        "total_calls": aggregate.total_calls,
        "ok_calls": aggregate.ok_calls,
        "fallback_calls": aggregate.fallback_calls,
        "input_tokens": aggregate.input_tokens,
        "output_tokens": aggregate.output_tokens,
        "est_cost_usd": aggregate.est_cost_usd,
        "avg_latency_ms": aggregate.avg_latency_ms,
        "per_status": aggregate.per_status,
        "per_model": aggregate.per_model,
        "first_ts": aggregate.first_ts,
        "last_ts": aggregate.last_ts,
    }
    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        window = f"last {aggregate.days} day(s)" if aggregate.days else "lifetime"
        print(
            f"LLM usage ({window}) · calls={aggregate.total_calls} · "
            f"ok={aggregate.ok_calls} · fallback={aggregate.fallback_calls} · "
            f"tokens={aggregate.input_tokens}/{aggregate.output_tokens} · "
            f"est_cost_usd={aggregate.est_cost_usd:.4f}"
        )
        if aggregate.per_status:
            statuses = ", ".join(
                f"{status}={count}" for status, count in sorted(aggregate.per_status.items())
            )
            print(f"statuses: {statuses}")
        if aggregate.per_model:
            models = ", ".join(
                f"{model}={count}" for model, count in sorted(aggregate.per_model.items())
            )
            print(f"models: {models}")
    return 0


def _synthesize(payloads: dict[str, AdapterPayload | None]) -> dict[str, Any]:
    return {
        "policy": synthesize_policy(payloads.get("super_pricing")),
        "inventory": synthesize_inventory(payloads.get("super_pricing")),
        "etf_flow": synthesize_etf_flow(payloads.get("etf_512400"), payloads.get("quant_trading")),
        "industry": synthesize_industry(payloads.get("quant_trading")),
        "observation": synthesize_observation(
            payloads.get("super_pricing"),
            payloads.get("quant_trading"),
            payloads.get("index_research"),
            payloads.get("etf_512400"),
        ),
    }


def _observation_raw_text(observation: dict[str, Any]) -> str:
    raw_text = observation.get("raw_text")
    if isinstance(raw_text, str):
        return raw_text
    return "\n".join(str(s) for s in observation.get("sentences") or [])


def _llm_render_context(
    raw_text: str,
    *,
    requested: bool,
    used: bool,
    status: str,
    model: str | None = None,
    latency_ms: float | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    note: str | None = None,
) -> dict[str, Any]:
    return {
        "requested": requested,
        "used": used,
        "status": status,
        "model": model,
        "latency_ms": latency_ms,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "raw_hash": hashlib.sha256(raw_text.encode("utf-8")).hexdigest()[:16]
        if raw_text
        else "",
        "note": note,
    }


def _maybe_rephrase_observation(
    observation: dict[str, Any],
    *,
    date: str,
    args: argparse.Namespace,
) -> dict[str, Any]:
    raw_text = _observation_raw_text(observation)
    observation.pop("polished_text", None)

    if not getattr(args, "with_llm", False):
        return _llm_render_context(
            raw_text,
            requested=False,
            used=False,
            status="disabled",
        )

    model = str(getattr(args, "llm_model", DEFAULT_LLM_MODEL))
    if not observation.get("available"):
        return _llm_render_context(
            raw_text,
            requested=True,
            used=False,
            status="skipped_no_signal",
            model=model,
            note="observation unavailable; using deterministic missing-data text",
        )

    result = rephrase_observation(
        raw_text,
        {
            "date": date,
            "industries": observation.get("industries") or [],
        },
        model=model,
    )
    log_usage(
        result,
        Path(getattr(args, "llm_usage_log", DEFAULT_LLM_USAGE_LOG)),
        extra={"date": date, "section": "observation"},
    )

    if result.ok:
        observation["polished_text"] = result.polished_text

    return _llm_render_context(
        raw_text,
        requested=True,
        used=result.ok,
        status=result.status,
        model=result.llm_model_used or model,
        latency_ms=result.latency_ms,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        note=result.note,
    )


def _inject_og_frontmatter(brief_path: Path, og_tags: dict[str, str]) -> None:
    """Merge OG metadata into the YAML frontmatter of a brief markdown file.

    The generator already writes a ``---`` block at the top of every
    brief with deterministic synthesis metadata (date, llm_status,
    observation_raw_hash, ...). We add a small set of ``og_*`` keys
    that the Jekyll layout reads through ``page.og_*`` Liquid lookups,
    keeping the canonical Markdown round-trippable.

    Existing ``og_*`` keys are overwritten so re-running ``generate``
    on the same date refreshes the metadata. Non-OG keys are
    preserved verbatim.
    """
    if not brief_path.exists():
        return
    text = brief_path.read_text(encoding="utf-8")
    new_keys = {
        "og_title": og_tags.get("og:title", ""),
        "og_description": og_tags.get("og:description", ""),
        "og_url": og_tags.get("og:url", ""),
        "og_image": og_tags.get("og:image", ""),
        "og_locale": og_tags.get("og:locale", "zh_CN"),
        "og_section": og_tags.get("article:section", ""),
        "twitter_handle": og_tags.get("twitter:site", ""),
        "article_published": og_tags.get("article:published_time", ""),
        # The Jekyll layout uses 'layout: brief' for per-brief pages;
        # we set this explicitly so a brief opened directly (without
        # going through the publisher's overlay) still picks up the
        # OG-aware layout.
        "layout": "brief",
    }
    lines = text.splitlines()
    if lines and lines[0].strip() == "---":
        end_idx = None
        for i, ln in enumerate(lines[1:], start=1):
            if ln.strip() == "---":
                end_idx = i
                break
        if end_idx is not None:
            existing = lines[1:end_idx]
            # Strip pre-existing og_* / layout keys; we'll re-add ours.
            kept = []
            stripped_keys = set(new_keys.keys())
            for ln in existing:
                key = ln.split(":", 1)[0].strip() if ":" in ln else ""
                if key in stripped_keys:
                    continue
                kept.append(ln)
            new_lines = ["---", *kept]
            for k, v in new_keys.items():
                new_lines.append(f"{k}: {_yaml_string_scalar(v)}")
            new_lines.append("---")
            new_lines.extend(lines[end_idx + 1 :])
            brief_path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
            return
    # No frontmatter — prepend one.
    head_lines = ["---"]
    for k, v in new_keys.items():
        head_lines.append(f"{k}: {_yaml_string_scalar(v)}")
    head_lines.append("---")
    brief_path.write_text(
        "\n".join(head_lines) + "\n" + text, encoding="utf-8"
    )


def _yaml_string_scalar(value: object) -> str:
    """Serialize a frontmatter value as a YAML-safe quoted string scalar."""
    text = "" if value is None else str(value)
    return json.dumps(text, ensure_ascii=False)


def _refresh_latest_symlink(briefs_dir: Path, brief_path: Path) -> None:
    """Atomically refresh ``briefs_dir/latest.md`` to point at ``brief_path``.

    On platforms without symlink support (rare on macOS but possible under
    some Windows configs) we silently fall back to copying the markdown
    content, so the file still exists. Errors are swallowed — the symlink
    is a convenience, not a correctness invariant.
    """
    latest = briefs_dir / "latest.md"
    target = brief_path.name  # relative within briefs_dir
    try:
        if latest.is_symlink() or latest.exists():
            latest.unlink()
        latest.symlink_to(target)
    except OSError:
        # Fallback: write the content directly. Better a stale file than
        # crashing the daily run on a filesystem that disallows symlinks.
        try:
            latest.write_text(brief_path.read_text(encoding="utf-8"), encoding="utf-8")
        except OSError:
            pass


def _relative_to(briefs_dir: Path, chart_path: Path) -> str:
    """Compute a markdown-friendly relative link from brief to chart.

    Charts live at output/charts/YYYY-MM-DD/foo.png and briefs at
    output/briefs/YYYY-MM-DD.md, so the relative path is typically
    `../charts/YYYY-MM-DD/foo.png`.
    """
    try:
        return str(Path("..") / chart_path.relative_to(briefs_dir.parent))
    except ValueError:
        return str(chart_path)


def _cmd_publish(args: argparse.Namespace) -> int:
    """v0.6 — push the brief for ``args.date`` to the gh-pages branch."""
    date = args.date or _today_cadence_date()

    if not args.dry_run:
        validation_results = _publish_preflight_checks()
        validation_code = summarize(validation_results)
        if validation_code == EXIT_FAIL:
            print("ERROR: publish blocked by data validation failures:", file=sys.stderr)
            for result in validation_results:
                if result.level == "fail":
                    print(f"  - {result.name}: {result.message}", file=sys.stderr)
            print("Run `cn-altdata-brief validate --json` for full details.", file=sys.stderr)
            return 2

    publisher = GhPagesPublisher(
        brief_dir=Path(args.briefs_dir),
        chart_dir=Path(args.charts_dir),
        feed_path=Path(args.feed_path),
        atom_path=Path(args.atom_path) if getattr(args, "atom_path", None) else None,
        template_dir=Path(args.template_dir),
        repo_root=Path(args.repo_root),
        gh_pages_branch=args.gh_pages_branch,
        digest_dir=Path(args.digests_dir) if getattr(args, "digests_dir", None) else None,
    )

    try:
        result = publisher.publish(
            date,
            push=not args.no_push,
            dry_run=args.dry_run,
        )
    except PublishError as exc:
        print(f"ERROR: publish failed: {exc}", file=sys.stderr)
        return 3

    if result.dry_run:
        print("=== DRY RUN ===")
        print(result.message)
        print()
        print("Files that would be copied:")
        for p in result.plan.files_to_copy:
            print(f"  · {p}")
        print()
        print(f"Branch: {result.plan.branch}"
              f"{'  [will create as orphan]' if result.plan.will_create_orphan else ''}")
        print(f"Index.md would list {len(result.plan.index_briefs)} brief(s):")
        for stem in result.plan.index_briefs[:10]:
            print(f"  · {stem}")
        if len(result.plan.index_briefs) > 10:
            print(f"  · … +{len(result.plan.index_briefs) - 10} more")
        print()
        print("Re-run without --dry-run to actually publish.")
        return 0

    push_tag = "pushed" if result.pushed else "committed (NOT pushed)"
    print(f"OK · {result.message} · {push_tag} · "
          f"returned to branch={result.original_branch or 'detached'}")
    return 0


def _publish_preflight_checks() -> list[Any]:
    """Run the same default validation gate before mutating gh-pages."""
    cfg = load_source_config(**source_mode_to_kwargs("auto"))
    payloads: dict[str, AdapterPayload | None] = {}
    for name, adapter in build_default_adapters(config=cfg).items():
        try:
            payloads[name] = adapter.fetch()
        except AdapterError as exc:
            logger.warning("publish preflight: adapter %s unavailable: %s", name, exc)
            payloads[name] = None
    return run_all_checks(payloads)


def _cmd_weekly_digest(args: argparse.Namespace) -> int:
    """v0.9 — aggregate this week's daily briefs into a single digest."""
    briefs_dir = Path(args.briefs_dir)
    digests_dir = Path(args.digests_dir)
    digests_dir.mkdir(parents=True, exist_ok=True)

    anchor_str = args.week_of or _today_cadence_date()
    try:
        anchor = datetime.strptime(anchor_str, "%Y-%m-%d").date()
    except ValueError:
        print(
            f"ERROR: --week-of must be YYYY-MM-DD, got {anchor_str!r}",
            file=sys.stderr,
        )
        return 2

    monday, friday, week_num, iso_year = iso_week_bounds(anchor)
    brief_paths = collect_brief_paths_for_week(briefs_dir, anchor)
    if not brief_paths:
        print(
            f"WARN: no daily briefs found in {briefs_dir} for week "
            f"{monday}→{friday}; writing degraded digest with notes.",
            file=sys.stderr,
        )

    digest = compose_weekly_digest(
        brief_paths,
        anchor=anchor,
        recurrence_threshold=max(1, int(args.recurrence_threshold)),
        now=datetime.now(UTC),
    )
    markdown = render_weekly_digest_markdown(context=digest.render_context())

    default_filename = f"{iso_year}-W{week_num:02d}.md"
    output_path = Path(args.output) if args.output else (digests_dir / default_filename)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(markdown, encoding="utf-8")

    en_path: Path | None = None
    en_status: str | None = None
    if getattr(args, "with_llm", False):
        en_path, en_status = _produce_digest_translation(
            digest_path=output_path,
            iso_year=iso_year,
            week_num=week_num,
            args=args,
        )

    parts = [
        f"OK · wrote {output_path}",
        f"week={iso_year}-W{week_num:02d}",
        f"briefs={digest.brief_count}/5",
        f"themes={len(digest.themes)}",
        f"inflections={len(digest.inflections)}",
    ]
    if en_path is not None:
        parts.append(f"en={en_path.name}({en_status})")
    print(" · ".join(parts))
    return 0


def _produce_digest_translation(
    *,
    digest_path: Path,
    iso_year: int,
    week_num: int,
    args: argparse.Namespace,
) -> tuple[Path, str]:
    """Translate a finished weekly digest into English using v0.8 infra.

    The output sits next to the CN digest as ``<base>.en.md`` (e.g.
    ``2026-W20.en.md``). On any failure the file is still written using
    the same fallback-banner contract as the daily brief.
    """
    source_md = digest_path.read_text(encoding="utf-8")
    model = str(getattr(args, "llm_model", DEFAULT_LLM_MODEL))
    result = translate_brief(source_md, target_language="en", model=model)
    en_path = digest_path.with_suffix(".en.md")
    en_path.write_text(result.translated_md, encoding="utf-8")
    _log_translation_usage(
        result,
        args=args,
        date=f"{iso_year}-W{week_num:02d}",
        language="EN",
    )
    return en_path, result.status


def _cmd_monthly_digest(args: argparse.Namespace) -> int:
    """v0.11 — aggregate a calendar month's dailies + weeklies into one monthly digest."""
    briefs_dir = Path(args.briefs_dir)
    digests_dir = Path(args.digests_dir)
    digests_dir.mkdir(parents=True, exist_ok=True)

    anchor = _resolve_month_anchor(args)
    if anchor is None:
        return 2  # error already printed
    first, last, label = month_bounds(anchor)

    brief_paths = collect_brief_paths_for_month(briefs_dir, anchor)
    digest_paths = collect_digest_paths_for_month(digests_dir, anchor)
    if not brief_paths:
        print(
            f"WARN: no daily briefs found in {briefs_dir} for month "
            f"{label} ({first}→{last}); writing degraded monthly digest.",
            file=sys.stderr,
        )

    monthly = compose_monthly_digest(
        brief_paths,
        digest_paths,
        anchor=anchor,
        sustained_threshold=max(1, int(args.sustained_threshold)),
        now=datetime.now(UTC),
    )
    markdown = render_monthly_digest_markdown(context=monthly.render_context())

    default_filename = f"{label}.md"
    output_path = Path(args.output) if args.output else (digests_dir / default_filename)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(markdown, encoding="utf-8")

    en_path: Path | None = None
    en_status: str | None = None
    if getattr(args, "with_llm", False):
        en_path, en_status = _produce_monthly_translation(
            digest_path=output_path,
            month_label=label,
            args=args,
        )

    parts = [
        f"OK · wrote {output_path}",
        f"month={label}",
        f"briefs={monthly.brief_count}",
        f"weeklies={monthly.digest_count}",
        f"sustained_themes={len(monthly.sustained_themes)}",
        f"reversals={len(monthly.reversal_events)}",
    ]
    if en_path is not None:
        parts.append(f"en={en_path.name}({en_status})")
    print(" · ".join(parts))
    return 0


def _resolve_month_anchor(args: argparse.Namespace):
    """Turn ``--month-of`` (or its absence) into a concrete date inside the target month.

    Accepts ``YYYY-MM`` for convenience and ``YYYY-MM-DD`` for symmetry
    with ``weekly-digest --week-of``. When omitted, defaults to LAST
    month — matching the "first business day of next month" cron
    contract. Returns ``None`` on parse failure after printing a
    diagnostic; the caller turns that into exit code 2.
    """
    from datetime import date as date_cls

    raw = getattr(args, "month_of", None)
    if raw is None:
        today = datetime.now(DEFAULT_CADENCE_TZ).date()
        return previous_month(today)
    raw = str(raw).strip()
    if len(raw) == 7 and raw[4] == "-":
        try:
            year = int(raw[:4])
            month = int(raw[5:])
            return date_cls(year, month, 1)
        except ValueError:
            pass
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date()
    except ValueError:
        print(
            f"ERROR: --month-of must be YYYY-MM or YYYY-MM-DD, got {raw!r}",
            file=sys.stderr,
        )
        return None


def _produce_monthly_translation(
    *,
    digest_path: Path,
    month_label: str,
    args: argparse.Namespace,
) -> tuple[Path, str]:
    """Translate a finished monthly digest into English using v0.8 infra.

    Output sits next to the CN digest as ``<YYYY-MM>.en.md``. Failures
    still write a file (banner contract) so the gh-pages publisher can
    always link to it.
    """
    source_md = digest_path.read_text(encoding="utf-8")
    model = str(getattr(args, "llm_model", DEFAULT_LLM_MODEL))
    result = translate_brief(source_md, target_language="en", model=model)
    en_path = digest_path.with_suffix(".en.md")
    en_path.write_text(result.translated_md, encoding="utf-8")
    _log_translation_usage(
        result,
        args=args,
        date=month_label,
        language="EN",
    )
    return en_path, result.status


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
