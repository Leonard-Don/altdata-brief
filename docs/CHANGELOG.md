# Changelog

## v0.11 — 2026-05-18

**feat: v0.11 — monthly digest (1st business day cadence)**

* `src/altdata_brief/digest/monthly.py` — new
  `compose_monthly_digest()` entry point and `MonthlyDigest` /
  `SustainedTheme` / `ReversalEvent` / `ETFMonthlySummary` /
  `WeeklyDigestSummary` dataclasses. Aggregates a full calendar
  month's daily briefs (~20) + intersecting weekly digests (~4) into
  a single deterministic monthly digest. **No LLM in the core
  synthesis** — the digest body is fully reproducible from the
  on-disk briefs + weekly digests.
* `templates/monthly_digest.md.j2` — new Jinja template with sections
  本月核心信号 / 月度核心主题 / 信号反转事件 / 行业累计影响排行 / ETF 资金流月度变化 /
  下月观察 + footer tables for both daily briefs and weekly digests
  that contributed.
* CLI: new `monthly-digest` subcommand with `--month-of` (accepts
  both `YYYY-MM` and `YYYY-MM-DD`), `--briefs-dir`, `--digests-dir`,
  `--output`, `--sustained-threshold`, `--with-llm`, `--llm-model`,
  `--llm-usage-log`. Default output path is
  `output/digests/<YYYY-MM>.md`. Default month is LAST month so the
  1st-of-month run produces "上月回顾".
* GitHub Pages publisher: now copies monthly digests to
  `gh-pages:digests/` alongside weekly digests (same directory,
  filename shape disambiguates: `2026-04` for monthly,
  `2026-W18` for weekly). Index page renders **three** tables:
  daily briefs, weekly digests, **monthly digests**.
* RSS / Atom feeds: monthly digest items use a `[Monthly]` title
  prefix, `altdata-brief:monthly:` GUID prefix, and
  `<category>monthly-digest</category>` (RSS) /
  `<category term="monthly-digest"/>` (Atom). Pub date pinned to the
  last day of the month at 17:00 UTC so the merged feed orders
  daily / weekly / monthly chronologically.
* macOS launchd: `scripts/install_launchd_macos.sh` now installs a
  **third** LaunchAgent `com.leonardodon.altdata-brief.monthly`
  firing every `Day=1` at 17:00. The uninstaller cleans up all
  three.
* `scripts/monthly_digest_now.sh` — manual / launchd wrapper that
  mirrors `weekly_digest_now.sh` (`uv sync` → `monthly-digest` →
  chained `publish_now.sh`). When the 1st is Sat/Sun the wrapper
  defers to the next Monday (override with
  `MONTHLY_DEFER_WEEKENDS=0`).
* Tests: 20 new tests under `tests/test_monthly_digest.py` (date
  helpers, path collection, parsing, sustained-theme detection,
  reversal events with flip counts, cumulative impact aggregation,
  ETF high/low, carry-forward forecast, empty input, CLI surface,
  launchd plist). Total suite: **195 tests, all green**.
* `pyproject.toml` and `__init__.py` bumped to 0.11.0.

### Monthly digest contract

* Sustained themes filtered at `--sustained-threshold` (default 12
  distinct days). Industries / metals below the bar are dropped.
* Reversal events count **every** sign flip in the month (not just
  the last), surfaced as `flips_in_month` and used as the primary
  sort key.
* Carry-forward forecast fires when a sustained theme's last-week
  occurrence count is ≥3 (controlled by
  `CARRY_FORWARD_LAST_WEEK_THRESHOLD`).
* ETF month-over-month summary: first-day / last-day / high-day /
  low-day with their daily NAV % and dates.
* Missing days degrade gracefully — a sparse month still produces a
  digest with `note` bullets flagging the small sample.

## v0.9 — 2026-05-17

**feat: v0.9 — weekly digest generator (Friday cadence)**

* `src/altdata_brief/digest/weekly.py` — new `compose_weekly_digest()`
  entry point and `WeeklyDigest` / `Theme` / `Inflection` /
  `DailyBriefSummary` dataclasses. Parses the deterministic CN daily
  briefs (Mon-Fri) and aggregates into themes (≥3 days),
  inflections (mid-week sign flips), cumulative industry impact,
  and ETF netflow. **No LLM in the core synthesis** — the
  digest body is fully reproducible from the same 5 daily-brief
  markdown files.
* `templates/weekly_digest.md.j2` — new Jinja template with sections
  本周核心信号 / 本周核心主题 / 信号反转 / 行业累计影响 / ETF 资金流摘要 /
  下周展望 + a constituents table that links back to each
  source daily brief.
* CLI: new `weekly-digest` subcommand with `--week-of`, `--briefs-dir`,
  `--digests-dir`, `--output`, `--recurrence-threshold`, `--with-llm`,
  `--llm-model`, `--llm-usage-log`. Default output path is
  `output/digests/<iso_year>-W<week>.md`. `--with-llm` re-uses the
  v0.8 translator to emit an `<base>.en.md` sibling — falls back to
  CN-with-banner if the SDK / API key is missing.
* GitHub Pages publisher: `GhPagesPublisher` now takes an optional
  `digest_dir`. Every digest md inside is shipped to
  `gh-pages:digests/`. The index renderer gains a second section
  `## 本周回顾 / Weekly digests` with the same CN / EN columns.
* RSS feed: `render_feed()` accepts an optional `digests_dir`. Weekly
  digest items use a `[Weekly]` title prefix, `altdata-brief:digest:`
  GUID, and `<category>weekly-digest</category>` so subscribers can
  filter cadence as well as language. Daily `generate` auto-includes
  the `digests/` dir when it exists.
* macOS launchd: `scripts/install_launchd_macos.sh` now installs a
  second LaunchAgent `com.leonardodon.altdata-brief.weekly`
  firing Friday 18:00 (an hour after the daily). The uninstaller
  cleans up both.
* `scripts/weekly_digest_now.sh` — manual / launchd wrapper that
  mirrors `run_now.sh`: `uv sync` → `weekly-digest` →
  `publish_now.sh` (opt-out with `RUN_PUBLISH_AFTER_DIGEST=0`).
* Tests: 20 new tests under `tests/test_weekly_digest.py` (parsing,
  theme detection, inflection detection, cumulative impact, empty
  input, template render, CLI surface, launchd plist). 1 new
  publisher test (`test_v09_digests_published_alongside_briefs`)
  and 1 new RSS test (`test_render_feed_merges_weekly_digests`).
  Total suite: 155 tests, all green.
* `pyproject.toml` and `__init__.py` bumped to 0.9.0. New
  `output/digests/` directory tracked in the layout.

### Digest contract

* Themes are filtered at `recurrence_threshold` (default 3 distinct
  weekdays). Industries / metals below the bar are dropped.
* Inflections are sign flips of `avg_impact` (政策) or `price_change_pct`
  (库存) between consecutive non-zero observations.
* "Forecast" bullets fire when a theme persists ≥4 days OR an
  inflection's last flip date is Thu/Fri.
* Missing days degrade gracefully — a 3-of-5 week still produces a
  digest, with a `note` flagging the short-week situation.

### What was deferred to v1.0

* LLM-driven narrative paragraph above the deterministic sections
  ("this week was characterized by …"). Today only the EN sibling
  uses the LLM, and only as a translation.
* Per-theme matplotlib sparklines embedded in the digest body.
* Cross-week / month-over-month comparisons.

---

## v0.8 — 2026-05-17

**feat: v0.8 — bilingual EN translation via Claude API**

* `src/altdata_brief/llm/translate.py` — new `translate_brief()`
  entry point and `TranslationResult` dataclass; CN brief stays the
  ground truth and the EN translation is an additive side-channel.
* `src/altdata_brief/llm/industry_mapping.json` — hand-curated
  CN→EN mapping for 45 industries, 18 commodities, 6 instruments,
  6 section headings, and 25 high-frequency phrases. Used both as
  glossary in the prompt and as the validation guard target.
* CLI: new `--languages CN,EN` flag on `generate`. Each language
  becomes its own file (`output/briefs/YYYY-MM-DD.md` for CN,
  `YYYY-MM-DD.en.md` for EN). Unsupported codes fail loudly.
* GitHub Pages publisher: copies `*.en.md` siblings, adds a third
  column to the index (`日期 / Date | 中文 / Chinese | English`),
  rewrites the template to advertise the bilingual format.
* RSS feed: one `<item>` per language per date. EN items are
  title-prefixed `[EN]`, get a `:en` GUID suffix, and carry a
  per-item `<language>en</language>` tag.
* Tests: 11 new tests under `tests/test_translation.py` (mock the
  Anthropic SDK end-to-end; no real API calls). Total suite: 135
  tests, all green.
* `pyproject.toml` bumped to 0.8.0; `industry_mapping.json` packaged
  as data so installed wheels carry the mapping.

### Bilingual contract

* CN is ALWAYS produced (deterministic).
* EN is requested via `--languages CN,EN` (or `EN` shorthand auto-
  prepends CN). When the SDK or API key is absent, or validation
  fails, the EN file is still written — with a banner explaining the
  fallback and the original CN content beneath, so URL subscribers
  never get a 404.
* Validation: every number in the CN source must survive in EN; every
  bolded CN industry name must map to its glossary English term (or
  remain untranslated as a deliberate fallback).

### Cost estimate

* Per bilingual day: ~1500 input + ~1000 output tokens →
  ~$0.0195 / day on claude-3-5-sonnet pricing (≈$7 / year if run
  365 days). Tracked per call in `output/llm_usage.jsonl`.

### What was deferred to v0.9

* Caching / dedup of identical CN inputs (skip the API call when
  `source_hash` is unchanged from the previous publish).
* Additional languages (`JP`, `KR`, `ES`) — scaffolding is in place;
  add the language code + suffix mapping in `cli.py` and extend
  `industry_mapping.json` with the target-language glossary.

---

## v0.6 — 2026-05-17

**feat: v0.6 — gh-pages publisher + Jekyll site**

* `src/altdata_brief/publish/gh_pages.py` — new `GhPagesPublisher`
  class that drives the publish pipeline via subprocess + git CLI
  (no GitPython dependency).
* `gh-pages-template/` — Jekyll scaffolding (`_config.yml`,
  `_layouts/brief.html`, `index.md`) overlaid onto the published
  branch on every run.
* CLI: new `publish` subcommand with `--dry-run`, `--no-push`,
  `--gh-pages-branch`, `--date`, `--repo-root`, `--template-dir`.
* `scripts/publish_now.sh` — manual / launchd-friendly wrapper.
* `scripts/run_now.sh` — chains `publish_now.sh` after a successful
  generate (toggle via `RUN_PUBLISH_AFTER_GENERATE=0`).
* RSS feed copied to gh-pages on every publish; index links to it.
* `docs/PUBLISHING.md` — step-by-step setup, troubleshooting,
  one-time user actions (`gh repo create`, Settings → Pages).
* Tests: 11 new tests under `tests/test_gh_pages_publisher.py`;
  total suite 114 passing.

### Atomic guarantees

* Refuses to publish if the source branch has uncommitted changes.
* On any subprocess failure, switches back to the original branch
  via `git checkout -f` (cleanliness is checked at the top, so
  forcing is safe).
* Dry-run path never touches git state.
* Idempotent: republishing the same date with frozen content
  produces no commit.

### What was deferred to v0.7

* LLM rephrase layer for the "本日观察" section.
* Optional external publishing adapters.
* Feed delivery reliability checks.
* Reader-facing metadata without paid-tier assumptions.

---

## v0.5 — 2026-05-16

* macOS launchd installer (`scripts/install_launchd_macos.sh`).
* Stable `latest.md` symlink for external readers.
* Failure notifications via `osascript display notification`.
* `scripts/run_now.sh` manual / launchd entry point.

## v0.4 — 2026-05

* All 4 adapters on public summary mode.
* Local e2e smoke test (`scripts/smoke_e2e.sh`).
* `resolve_source()` adapter introspection.

## v0.3 — 2026-05

* `data/public/*_summary.json` preferred over cache.
* `--source-mode {auto,public,cache,live}` CLI flag.
* GitHub Actions sandbox can render the brief.

## v0.2 — 2026-05

* `validate` subcommand for data-quality preconditions.
* RSS feed (`output/feed.xml`).
* 3-sentence "本日观察" section.

## v0.1 — 2026-05

* Initial scaffold: 4 cache sources, 5-section daily brief, 4 charts.
* GitHub Actions template (not yet used for the live run).
