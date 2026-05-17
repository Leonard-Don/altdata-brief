"""``cn-altdata-brief`` — single CLI entrypoint.

Usage::

    cn-altdata-brief generate              # generate today's brief
    cn-altdata-brief generate --date 2026-05-17
    cn-altdata-brief generate --output-dir /tmp/briefs
    cn-altdata-brief validate              # run data-quality preconditions
    cn-altdata-brief validate --fail-on-warn  # CI mode
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cn_altdata_brief import __version__
from cn_altdata_brief.adapters import build_default_adapters
from cn_altdata_brief.adapters.base import AdapterPayload, AdapterUnavailable
from cn_altdata_brief.config import (
    load_source_config,
    source_mode_to_kwargs,
)
from cn_altdata_brief.render import render_all_charts, render_brief_markdown, render_site_index
from cn_altdata_brief.render.rss import render_feed
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

SOURCE_MODE_CHOICES = ("auto", "public", "cache", "live")

PROJECT_ROOT = Path(__file__).resolve().parents[2]  # src/cn_altdata_brief/cli.py -> project root
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "output"
DEFAULT_BRIEFS_DIR = DEFAULT_OUTPUT_DIR / "briefs"
DEFAULT_CHARTS_DIR = DEFAULT_OUTPUT_DIR / "charts"

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
        default="https://example.github.io/cn-altdata-brief",
        help="Base URL used for RSS <link> elements (default: placeholder).",
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
        "-v", "--verbose", action="store_true", help="Verbose logging (subcommand-scoped alias)."
    )

    return parser


# ---------------------------------------------------------------------------


def _cmd_generate(args: argparse.Namespace) -> int:
    date = args.date or datetime.now(UTC).strftime("%Y-%m-%d")
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
        except AdapterUnavailable as exc:
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
    }
    markdown = render_brief_markdown(context=context)
    brief_path = briefs_dir / f"{date}.md"
    brief_path.write_text(markdown, encoding="utf-8")

    # v0.5: maintain a stable `latest.md` symlink so external readers
    # (e.g. launchd cron jobs, RSS regenerators, publish branches) have
    # a fixed filename to point at without knowing today's date stamp.
    _refresh_latest_symlink(briefs_dir, brief_path)

    if not args.no_index:
        render_site_index(briefs_dir)

    feed_path: Path | None = None
    if not args.no_feed:
        feed_path = briefs_dir.parent / "feed.xml"
        render_feed(briefs_dir=briefs_dir, feed_path=feed_path, site_url=args.site_url)

    available_count = sum(1 for s in sections.values() if s.get("available"))
    feed_suffix = f" · feed={feed_path.name}" if feed_path else ""
    mode_summary = ",".join(
        f"{name}={(p.data.get('source_mode', '?') if p else 'missing')}"
        for name, p in payloads.items()
    )
    print(
        f"OK · wrote {brief_path} · {available_count}/5 sections available · "
        f"{len(chart_paths)} charts{feed_suffix} · mode={args.source_mode} · "
        f"resolved=[{mode_summary}] · sources: "
        + ", ".join(sorted(p.source for p in payloads.values() if p is not None))
    )
    return 0


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
        except AdapterUnavailable as exc:
            logger.warning("validate: adapter %s unavailable: %s", name, exc)
            payloads[name] = None

    results = run_all_checks(
        payloads,
        allow_missing_cache_only_sources=(source_mode == "public"),
    )
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
        common = Path(*chart_path.parts[: len(briefs_dir.parts)])  # naive trim
        _ = common
        return str(Path("..") / chart_path.relative_to(briefs_dir.parent))
    except ValueError:
        return str(chart_path)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
