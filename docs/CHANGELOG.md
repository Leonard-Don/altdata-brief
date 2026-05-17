# Changelog

## v0.8 — 2026-05-17

**feat: v0.8 — bilingual EN translation via Claude API**

* `src/cn_altdata_brief/llm/translate.py` — new `translate_brief()`
  entry point and `TranslationResult` dataclass; CN brief stays the
  ground truth and the EN translation is an additive side-channel.
* `src/cn_altdata_brief/llm/industry_mapping.json` — hand-curated
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

* `src/cn_altdata_brief/publish/gh_pages.py` — new `GhPagesPublisher`
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
* Substack auto-publish.
* Email subscriber pipeline.
* Analytics / paid-tier auth.

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
