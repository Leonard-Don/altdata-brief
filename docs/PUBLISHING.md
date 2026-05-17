# Publishing to GitHub Pages · v0.6+

> v0.8 adds bilingual (CN + EN) publishing — see section 7 below. The
> rest of this guide applies unchanged: CN is always the ground truth
> and the EN sibling rides through the same pipeline.

This guide walks through turning `cn-altdata-brief` from a "writes
markdown to my laptop" tool into a publicly readable static site at
`https://leonard-don.github.io/cn-altdata-brief/`.

The pipeline has three layers:

| Layer | Where | How often |
|---|---|---|
| Generate brief | local laptop (launchd or manual) | every weekday 17:00 CST |
| Push to `gh-pages` | `scripts/publish_now.sh` | chained after every generate |
| Render as HTML | GitHub Pages (Jekyll) | automatic on every push |

Upstream caches stay private — only the brief markdown / charts / RSS
are pushed to the public branch.

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
chart PNG under `output/charts/<date>/`, and `feed.xml`. If anything
is missing, run `uv run cn-altdata-brief generate` first.

---

## 2. Daily auto-publish flow

`scripts/run_now.sh` is the launchd entry point installed in v0.5. v0.6
makes it chain `publish_now.sh` automatically after a successful
generate:

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
                ├── copy brief + charts + feed.xml
                ├── overlay Jekyll template
                ├── regenerate index.md
                ├── commit + push origin gh-pages
                └── restore original branch
```

To opt out for a single run (e.g. on a plane):

```bash
RUN_PUBLISH_AFTER_GENERATE=0 bash scripts/run_now.sh
```

If `publish_now` fails the brief is still on disk — generate's exit
code is what propagates. A macOS notification surfaces the publish
failure for manual follow-up.

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
| RSS feed link returns 404 | `feed.xml` not in publish payload | Ensure `output/feed.xml` exists (don't pass `--no-feed` to `generate`) |

---

## 6. Why this design

* **Orphan branch**: keeps `main` history clean and lets us reset the
  public site without polluting source-of-truth commits.
* **No GitHub Actions**: the daily run is local-first (launchd from
  v0.5). Pushes from your laptop reuse your existing `gh auth` —
  nothing to configure server-side.
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
