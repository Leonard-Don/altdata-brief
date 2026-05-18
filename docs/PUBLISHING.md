# Publishing to GitHub Pages · v0.11

> v0.11 publishes the full cadence trilogy: daily briefs, weekly digests,
> and monthly digests. v0.8+ bilingual CN/EN siblings and v0.10 Atom feeds
> ride through the same publisher; CN remains the ground truth.

This guide walks through turning `cn-altdata-brief` from a "writes
markdown to my laptop" tool into a publicly readable static site at
`https://leonard-don.github.io/cn-altdata-brief/`.

The pipeline has three layers:

| Layer | Where | How often |
|---|---|---|
| Generate brief | local laptop (launchd or manual) | every weekday 17:00 CST |
| Generate CI smoke | GitHub Actions `Daily Brief` | every weekday 09:00 CST |
| Push to `gh-pages` | `scripts/publish_now.sh` or manual Actions `publish=true` | explicit opt-in |
| Render as HTML | GitHub Pages (Jekyll) | automatic on every push |

Upstream caches stay private — only generated briefs, charts, RSS/Atom
feeds, and digest markdown are pushed to the public branch.

---

## 1. One-time setup

### 1.1 Create the public repo (user action)

```bash
gh repo create leonard-don/cn-altdata-brief --public --source=. --remote=origin --push
```

If the repo already exists, just ensure the `origin` remote points at
the public URL:

```bash
git remote -v
# origin  https://github.com/Leonard-Don/cn-altdata-brief.git (fetch)
# origin  https://github.com/Leonard-Don/cn-altdata-brief.git (push)
```

### 1.2 Enable GitHub Pages (browser action)

1. Open `https://github.com/Leonard-Don/cn-altdata-brief/settings/pages`.
2. Under **Build and deployment** → **Source**, choose **Deploy from a branch**.
3. Branch = `gh-pages`, folder = `/ (root)`.
4. Click **Save**.

The first publish from `cn-altdata-brief publish` creates the
`gh-pages` branch as an orphan. Until that branch exists, the dropdown
won't list it — run a dry-run first to confirm the plan, then a real
publish, then enable Pages.

### 1.3 Verify locally before going live

```bash
uv run cn-altdata-brief publish --dry-run
```

You should see a file list including today's `briefs/<date>.md`, every
chart PNG under `output/charts/<date>/`, `feed.xml`, `feed.atom`, and any
weekly/monthly files under `output/digests/`. If anything is missing, run
`uv run cn-altdata-brief generate` first.

---

## 2. Daily auto-publish flow

`Daily Brief` in GitHub Actions is a safety smoke by default: scheduled
runs checkout the four public-source artifacts, run `validate` + `generate`
under `--source-mode public`, then run `cn-altdata-brief publish --dry-run`
to prove the publish payload is complete without mutating `gh-pages`.

Real publishing remains opt-in:

- local-first: `scripts/run_now.sh` chains `scripts/publish_now.sh` after a successful generate;
- GitHub Actions: manually run **Daily Brief** and set `publish=true`.

The local launchd path installed in v0.5 still looks like this:

```
launchd 17:00 CST
   ↓
scripts/run_now.sh
   ├── uv sync --quiet
   ├── uv run cn-altdata-brief generate --source-mode auto
   └── (if exit=0 and RUN_PUBLISH_AFTER_GENERATE != 0)
        scripts/publish_now.sh
            └── uv run cn-altdata-brief publish
                ├── checkout gh-pages
                ├── copy brief + charts + RSS/Atom + digests
                ├── overlay Jekyll template
                ├── regenerate index.md
                ├── commit + push origin gh-pages
                └── restore original branch
```

To opt out for a single local run (e.g. on a plane):

```bash
RUN_PUBLISH_AFTER_GENERATE=0 bash scripts/run_now.sh
```

If `publish_now` fails the brief is still on disk — generate's exit
code is what propagates. A macOS notification surfaces the publish
failure for manual follow-up.

For Actions manual publishing, the workflow first fetches
`origin/gh-pages:gh-pages` if it exists, then calls the same CLI publisher.
This avoids the old hand-copy template that missed newer artifacts such as
`feed.atom`, EN siblings, weekly digests, and monthly digests.

---

## 3. Manual operations

### Publish a specific past brief

```bash
PUBLISH_DATE=2026-05-16 bash scripts/publish_now.sh
# or:
uv run cn-altdata-brief publish --date 2026-05-16
```

### Plan-only run (no git mutation)

```bash
uv run cn-altdata-brief publish --dry-run
```

### Local commit but no push (offline rehearsal)

```bash
uv run cn-altdata-brief publish --no-push
```

### Republish from scratch (delete orphan branch)

If the `gh-pages` history gets ugly and you want to recreate it as a
single commit:

```bash
git branch -D gh-pages 2>/dev/null || true
git push origin --delete gh-pages 2>/dev/null || true
uv run cn-altdata-brief publish     # will recreate as orphan
```

This rewrites public history — fine for a personal site, never do it
on a shared one.

---

## 4. Customizing the site

Edit `gh-pages-template/`. The publisher **overlays** every file in
that directory onto the worktree on every publish, so changes
propagate without manual sync.

| Path | What it controls |
|---|---|
| `_config.yml` | Jekyll theme, site title, plugins |
| `_layouts/brief.html` | HTML wrapper for each `briefs/<date>.md` |
| `index.md` | Landing-page stub (the publisher overwrites this with the live archive table) |

To switch themes, edit `_config.yml`'s `theme:` line and re-publish.
Defaults to `minima` (ships with `github-pages` gem, zero
configuration).

---

## 5. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `git push` fails: `Permission denied (publickey)` | SSH key not on GitHub | `gh auth login` or switch remote to HTTPS |
| Publish refuses with `uncommitted changes` | dirty working tree on `main` | `git status`, commit or stash first |
| Pages site is 404 after first publish | GitHub Pages source still pointing at wrong branch | Settings → Pages → Source = `gh-pages` |
| Charts render as broken images on Pages | Jekyll filtering by frontmatter | `_config.yml` includes `charts/` — check it didn't get edited |
| RSS/Atom feed link returns 404 | Feed file not in publish payload | Ensure `output/feed.xml` / `output/feed.atom` exists; scheduled Actions dry-run should list both when present |

---

## 6. Why this design

* **Orphan branch**: keeps `main` history clean and lets us reset the
  public site without polluting source-of-truth commits.
* **GitHub Actions stays non-mutating by default**: scheduled `Daily Brief`
  runs validate/generate plus a `publish --dry-run` payload check. A real
  `gh-pages` push requires a manual workflow dispatch with `publish=true`,
  and still goes through the same CLI publisher as local runs.
* **Template overlay**: edits to the Jekyll site live in `main` and
  flow forward on every publish, so the canonical config never lives
  only on the published branch.
* **Idempotent**: republishing the same date with no upstream changes
  is a no-op — the publisher detects an empty staged diff and skips
  the commit, returning a "no changes" result.

---

## 7. Bilingual publishing (v0.8+)

From v0.8, `generate` can produce both Chinese (deterministic ground
truth) and English (LLM translation) versions of the same brief.

### 7.1 What changes operationally

* `output/briefs/2026-05-17.md` — CN, exactly as before.
* `output/briefs/2026-05-17.en.md` — new EN sibling, frontmatter
  carries `language: "en"` + `translation_status: ok | validation_failed | ...`.
* The publisher picks up every `*.en.md` next to the CN file and copies
  them into `gh-pages/briefs/`. No script edits required.
* `index.md` gains a third column on the homepage table:
  `日期 / Date | 中文 / Chinese | English`. Dates without an EN
  translation render an em-dash in the EN column.
* RSS: one `<item>` per language per date. EN items use a `[EN]`
  title prefix, a `:en` GUID suffix, and a per-item `<language>en</language>`
  tag so subscribers can filter.

### 7.2 Daily flow with bilingual

```bash
# requires the [llm] extra and ANTHROPIC_API_KEY
uv sync --extra llm
export ANTHROPIC_API_KEY=...
uv run cn-altdata-brief generate --with-llm --languages CN,EN
bash scripts/publish_now.sh
```

### 7.3 Failure handling

Translation is best-effort: on SDK-missing, API-key-missing, network
error, or validation drift the `.en.md` is **still written** with the
Chinese source body and a banner explaining the fallback. This means
the EN URL never 404s — readers always see something, and the
frontmatter records the reason for the fallback for the operator. The
next scheduled run retries.

### 7.4 Cost / observability

Each translation call is logged to `output/llm_usage.jsonl` (same
file as the v0.7 rephrase log). Run `uv run cn-altdata-brief llm-usage`
to summarize lifetime / per-status cost. Per-day cost for bilingual is
~1500 input + ~1000 output tokens ≈ $0.02 at claude-3-5-sonnet rates.

---

## 8. Weekly digest publishing (v0.9+)

v0.9 introduces a Friday-evening **本周回顾 / Weekly digest** that
aggregates the past 5 daily briefs into a single longer-form
markdown. Cadence and audience differ from the daily brief:

| | Daily brief | Weekly digest |
|---|---|---|
| Cadence | Mon-Fri 17:00 | Fridays 18:00 |
| Surface | `output/briefs/YYYY-MM-DD.md` | `output/digests/YYYY-Wnn.md` |
| Synthesis | Deterministic + optional LLM rephrase of 本日观察 | Deterministic only (LLM is opt-in EN sibling) |
| RSS GUID | `cn-altdata-brief:<date>[:en]` | `cn-altdata-brief:digest:<stem>[:en]` |
| Index column | 简报列表 / Briefs archive | 本周回顾 / Weekly digests |

### 8.1 What changes operationally

* `output/digests/2026-W20.md` is a new artifact alongside the daily
  briefs. It is generated by `cn-altdata-brief weekly-digest`.
* The gh-pages publisher copies every digest it finds in
  `output/digests/` into `gh-pages:digests/`. Daily publishes also
  reuse the same path, so a Friday digest stays online through the
  subsequent Mon-Thu daily publishes.
* `index.md` gains a second section, `## 本周回顾 / Weekly digests`,
  with the same CN / EN column layout used for daily briefs.
* RSS items for digests are tagged `[Weekly] ...` in the title, carry
  a `:digest` GUID prefix, and include `<category>weekly-digest</category>`
  so subscribers can filter on cadence.

### 8.2 Friday flow

```
launchd Fri 17:00         → daily brief (Friday's last)
launchd Fri 18:00         → weekly digest
   ↓
scripts/weekly_digest_now.sh
   ├── uv sync --quiet
   ├── uv run cn-altdata-brief weekly-digest
   └── (if exit=0 and RUN_PUBLISH_AFTER_DIGEST != 0)
        scripts/publish_now.sh
            └── uv run cn-altdata-brief publish
                ├── checkout gh-pages
                ├── copy briefs + charts + RSS/Atom feeds + digests
                ├── overlay Jekyll template
                ├── regenerate index.md (now with digests section)
                ├── commit + push origin gh-pages
                └── restore original branch
```

### 8.3 Manual operations

```bash
# Generate digest for the current ISO week (Mon..Fri):
uv run cn-altdata-brief weekly-digest

# Generate for a back-dated week:
uv run cn-altdata-brief weekly-digest --week-of 2026-05-14

# Raise the recurrence bar (default 3 days) → fewer, stronger themes:
uv run cn-altdata-brief weekly-digest --recurrence-threshold 4

# Also emit an EN sibling (uses v0.8 translator + ANTHROPIC_API_KEY):
uv run cn-altdata-brief weekly-digest --with-llm

# Run the wrapper script the launchd job uses (without waiting for Friday):
bash scripts/weekly_digest_now.sh

# Opt out of the chained publish:
RUN_PUBLISH_AFTER_DIGEST=0 bash scripts/weekly_digest_now.sh
```

### 8.4 Install / uninstall the Friday job

```bash
# v0.11: installs ALL THREE launchd agents in one call (daily / weekly / monthly).
bash scripts/install_launchd_macos.sh

# Removes all three.
bash scripts/uninstall_launchd_macos.sh

# Verify queued jobs:
launchctl list | grep cn-altdata
#  com.leonardodon.cn-altdata-brief
#  com.leonardodon.cn-altdata-brief.weekly
#  com.leonardodon.cn-altdata-brief.monthly
```

### 8.5 What a digest contains

The deterministic synthesis surfaces five sections:

1. **本周核心主题** — industries / metals in ≥3 daily briefs (default
   threshold; lift it with `--recurrence-threshold`).
2. **信号反转** — (name, kind) pairs whose numeric sign flipped at
   least once during the week.
3. **行业累计影响** — sum of avg_impact for every industry seen this
   week, sorted by |total|.
4. **ETF 资金流摘要** — sum of daily NAV % moves across the 5 days.
5. **下周展望** — themes that persisted ≥4 days and inflections that
   fired Thu/Fri get flagged as worth watching next week.

No LLM call is made in any of these five sections — the synthesis is
fully reproducible from the same 5 daily-brief markdown files. The
`--with-llm` flag re-uses the v0.8 translator only to produce an EN
sibling.

## 9. v0.11 — 上月回顾 / Monthly digest (1st-of-month cadence)

v0.11 closes the cadence trilogy: daily (~17:00 weekdays) →
weekly (Friday 18:00) → **monthly (1st of month 17:00)**. Where the
weekly digest tracks *tactics*, the monthly digest tracks *strategic
frame*:

- **Multi-week sustained themes** — industries that appeared on
  ≥12 distinct trading days of the month (so they ran through
  multiple weeks, not just one).
- **Within-month reversal events** — the v0.9 inflection detector
  re-run over a ~20-day window, plus a `flips_in_month` count so a
  signal that wobbled all month outranks one that flipped once at
  month-end.
- **Long-term cumulative impact** — same primitive as the weekly, just
  with a longer window.
- **ETF month-over-month change** — first/last day moves, plus
  intramonth high/low (with dates).
- **下月观察** — deterministic "carry-forward" forecast: sustained
  themes whose last-week occurrence count is still ≥3.

### 9.1 Generate manually

```bash
# Default: aggregate LAST month (matches the 1st-of-next-month cadence).
uv run cn-altdata-brief monthly-digest

# Explicit month (YYYY-MM):
uv run cn-altdata-brief monthly-digest --month-of 2026-04

# Or a YYYY-MM-DD date inside the target month:
uv run cn-altdata-brief monthly-digest --month-of 2026-04-15

# Raise the sustained-theme bar (default 12 days) for harsher filtering:
uv run cn-altdata-brief monthly-digest --sustained-threshold 15

# Emit an EN sibling alongside the CN file (reuses v0.8 translator):
uv run cn-altdata-brief monthly-digest --with-llm
```

Outputs:

- `output/digests/<YYYY-MM>.md` (e.g. `2026-04.md`) — CN ground truth.
- `output/digests/<YYYY-MM>.en.md` — optional EN sibling.

The monthly digest sits in the **same** `output/digests/` directory as
the weekly digests; the filename shape (`2026-04` vs `2026-W18`)
disambiguates the cadence. Both are shipped to `gh-pages:digests/` on
every publish.

### 9.2 Schedule (launchd)

`scripts/install_launchd_macos.sh` now installs **three** LaunchAgents:

| Label | Cadence |
|---|---|
| `com.leonardodon.cn-altdata-brief` | Mon-Fri 17:00 (daily) |
| `com.leonardodon.cn-altdata-brief.weekly` | Fri 18:00 (weekly) |
| `com.leonardodon.cn-altdata-brief.monthly` | 1st of month 17:00 |

The monthly plist fires on every `Day=1`. The wrapper script
(`scripts/monthly_digest_now.sh`) detects Sat/Sun and **defers to the
next Monday** so the published monthly digest never lands on a
weekend. Override with `MONTHLY_DEFER_WEEKENDS=0`.

### 9.3 Run the wrapper without waiting

```bash
# Run the same script the launchd job runs (without waiting for the 1st):
bash scripts/monthly_digest_now.sh

# Back-fill an older month:
MONTHLY_OF=2026-03 bash scripts/monthly_digest_now.sh

# Don't chain publish (offline mode):
RUN_PUBLISH_AFTER_DIGEST=0 bash scripts/monthly_digest_now.sh
```

### 9.4 gh-pages integration

The publisher copies every `digests/<YYYY-MM>*.md` (and `<YYYY-Www>*.md`)
to the gh-pages branch and renders **three** tables on the landing
page:

1. **简报列表 / Briefs archive** — every daily brief with chart thumbnail.
2. **本周回顾 / Weekly digests** — every weekly digest.
3. **上月回顾 / Monthly digests** — every monthly digest. _(NEW in v0.11)_

The RSS / Atom feeds also include monthly items, with a
`[Monthly]` title prefix, `cn-altdata-brief:monthly:` GUID, and
`<category>monthly-digest</category>` so subscribers can filter on
cadence.
