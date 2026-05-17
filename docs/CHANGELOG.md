# Changelog

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
