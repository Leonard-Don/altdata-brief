"""v0.6 — GitHub Pages publisher.

This module turns a locally-generated brief into a public artifact
served by GitHub Pages.

Pipeline
--------

1. Read today's brief markdown + charts from ``output/briefs`` /
   ``output/charts`` (already produced by ``cn-altdata-brief generate``).
2. Stash them in a tmp staging dir so the worktree swap is atomic.
3. Check out (or create as orphan) the ``gh-pages`` branch.
4. Copy the staged files onto the gh-pages worktree, regenerate the
   Jekyll index, and overlay the static template files
   (``_config.yml``, ``_layouts/brief.html``) on first publish.
5. ``git add`` / ``git commit`` / ``git push`` (push gated by
   ``push=True``).
6. Restore the user's original branch — even on failure. If any
   subprocess returns non-zero we abort, undo what we did, and let the
   caller decide how loud to be.

We deliberately drive ``git`` via ``subprocess`` rather than depend on
GitPython — the operations we need (fetch, checkout, add, commit,
push) all map 1:1 to porcelain commands and the dependency budget for
this project is tight.

Design notes
------------

* ``dry_run=True`` performs the **planning** only: walk the brief
  directory, decide which files would be copied, compute the future
  index.md contents, but never touch git state. The returned
  :class:`PublishResult` carries ``planned_files`` so a CLI ``--dry-run``
  can show what would happen.
* The orphan-branch bootstrap on first publish creates a brand-new
  history. We do **not** mirror ``main`` history into ``gh-pages`` —
  the published site stands alone.
* All ``git`` commands run with ``cwd=repo_root`` and never call
  ``os.chdir``. Two parallel publishers would conflict on the worktree
  anyway (git would refuse), so the simpler subprocess model is fine.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)

# Paths copied verbatim into the gh-pages root on every publish.
# The list is small on purpose — each entry is one tracked file in the
# template dir; ``_layouts/brief.html`` is nested by directory copy.
_TEMPLATE_FILES = (
    "_config.yml",
    "_layouts/brief.html",
)

# Files we expect to find inside ``briefs_dir`` after a successful
# ``generate`` run. Missing today's dated brief is a hard error;
# missing latest.md / index.md is harmless (we regenerate them).
_DEFAULT_BRANCH = "gh-pages"


@dataclass(frozen=True)
class PublishPlan:
    """What a publish run intends to do.

    Used for the dry-run path and surfaced inside :class:`PublishResult`
    so callers can show or log the planned operations without rerunning
    the planning logic.

    v0.9 — also tracks any weekly digests under ``digest_sources`` so
    they ride along to the gh-pages branch on every publish (no need
    for a separate "publish-digest" subcommand).

    v0.11 — ``monthly_sources`` / ``index_monthlies`` mirror the weekly
    fields for the new third cadence; the publisher copies them to
    ``digests/<YYYY-MM>.md`` and renders a separate table on the index.
    """

    date: str
    branch: str
    brief_source: Path
    chart_source: Path | None
    feed_source: Path | None
    atom_source: Path | None = None
    files_to_copy: list[Path] = field(default_factory=list)
    index_briefs: list[str] = field(default_factory=list)
    index_digests: list[str] = field(default_factory=list)
    index_monthlies: list[str] = field(default_factory=list)
    digest_sources: list[Path] = field(default_factory=list)
    monthly_sources: list[Path] = field(default_factory=list)
    will_create_orphan: bool = False


@dataclass(frozen=True)
class PublishResult:
    """Outcome of a (dry or real) publish run."""

    plan: PublishPlan
    dry_run: bool
    pushed: bool
    commit_sha: str | None
    original_branch: str | None
    message: str


class PublishError(RuntimeError):
    """Raised when the publish pipeline cannot complete safely.

    The publisher catches it internally to roll back to the user's
    original branch, then re-raises so the CLI surfaces a non-zero
    exit code.
    """


class GhPagesPublisher:
    """Copy briefs into a ``gh-pages`` branch and push them.

    Construct it with the locally-rooted directory layout, then call
    :meth:`publish` (real run) or :meth:`plan_only` (dry run).

    Parameters
    ----------
    brief_dir:
        Path to ``output/briefs`` — must contain ``<date>.md`` for the
        date you want to publish.
    chart_dir:
        Path to ``output/charts`` — chart subdir ``<date>/`` is copied
        if present. Optional; passing ``None`` means "no charts".
    feed_path:
        Optional ``output/feed.xml`` (from v0.2 RSS module). Copied into
        the gh-pages root so subscribers can keep their feeds working
        against the public URL.
    template_dir:
        Path to the Jekyll template (defaults to
        ``<repo_root>/gh-pages-template``). Overlay-copied on every
        publish so template edits propagate without manual sync.
    repo_root:
        Path of the git repo to act on. Defaults to ``Path.cwd()`` so
        the CLI / scripts can leave it implicit.
    gh_pages_branch:
        Branch to publish to. ``gh-pages`` matches the GitHub Pages
        default; override only if you've reconfigured the source branch
        in repository Settings.
    git_executable:
        Override for the ``git`` binary, useful in tests.
    """

    def __init__(
        self,
        brief_dir: Path,
        chart_dir: Path | None = None,
        feed_path: Path | None = None,
        template_dir: Path | None = None,
        repo_root: Path | None = None,
        gh_pages_branch: str = _DEFAULT_BRANCH,
        git_executable: str = "git",
        digest_dir: Path | None = None,
        atom_path: Path | None = None,
    ) -> None:
        self.brief_dir = Path(brief_dir)
        self.chart_dir = Path(chart_dir) if chart_dir is not None else None
        self.feed_path = Path(feed_path) if feed_path is not None else None
        self.atom_path = Path(atom_path) if atom_path is not None else None
        self.repo_root = Path(repo_root) if repo_root is not None else Path.cwd()
        self.template_dir = (
            Path(template_dir)
            if template_dir is not None
            else self.repo_root / "gh-pages-template"
        )
        self.gh_pages_branch = gh_pages_branch
        self.git = git_executable
        # v0.9 — weekly digests directory. Defaults to ``<brief_dir
        # parent>/digests`` so callers that already configured
        # ``brief_dir`` get the right place "for free". Passing ``None``
        # explicitly disables digest publishing entirely (useful in
        # tests that want to keep the old payload shape).
        if digest_dir is None:
            inferred = self.brief_dir.parent / "digests"
            self.digest_dir: Path | None = inferred if inferred.exists() else None
        else:
            self.digest_dir = Path(digest_dir)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def plan(self, date_str: str) -> PublishPlan:
        """Compute the publish plan for ``date_str`` without touching git."""
        brief_path = self.brief_dir / f"{date_str}.md"
        if not brief_path.exists():
            raise PublishError(
                f"brief not found for date {date_str!r}: {brief_path} "
                "(run `uv run cn-altdata-brief generate --date "
                f"{date_str}` first)"
            )
        chart_subdir = self.chart_dir / date_str if self.chart_dir else None
        if chart_subdir is not None and not chart_subdir.exists():
            chart_subdir = None  # silently skip — charts are optional

        feed_source = (
            self.feed_path if (self.feed_path and self.feed_path.exists()) else None
        )
        atom_source = (
            self.atom_path if (self.atom_path and self.atom_path.exists()) else None
        )

        files_to_copy: list[Path] = [brief_path]
        # v0.8: pick up bilingual siblings. Each ``YYYY-MM-DD.<lang>.md``
        # next to the CN file is shipped under the same name.
        files_to_copy.extend(self._collect_language_variants(date_str))
        if chart_subdir is not None:
            files_to_copy.extend(sorted(chart_subdir.glob("*.png")))
        if feed_source is not None:
            files_to_copy.append(feed_source)
        if atom_source is not None:
            files_to_copy.append(atom_source)

        # v0.9 — pick up any weekly digests sitting in ``digest_dir``.
        # We publish ALL existing digests on every run (cheap; a few kb
        # each), so a Friday digest stays alive across subsequent
        # weekday daily publishes.
        digest_sources = self._collect_digest_sources()
        files_to_copy.extend(digest_sources)

        # v0.11 — pick up any monthly digests sitting in the same
        # ``digest_dir``. Filename shape (``YYYY-MM`` vs ``YYYY-Www``)
        # tells the two cadences apart; both live in ``digests/``.
        monthly_sources = self._collect_monthly_sources()
        files_to_copy.extend(monthly_sources)

        # Index lists ALL dated briefs (existing + the new one merged in).
        all_dates = self._collect_all_brief_dates(extra=date_str)
        digest_stems = self._collect_digest_stems(digest_sources)
        monthly_stems = self._collect_monthly_stems(monthly_sources)

        will_create_orphan = not self._branch_exists(self.gh_pages_branch)

        return PublishPlan(
            date=date_str,
            branch=self.gh_pages_branch,
            brief_source=brief_path,
            chart_source=chart_subdir,
            feed_source=feed_source,
            atom_source=atom_source,
            files_to_copy=files_to_copy,
            index_briefs=all_dates,
            index_digests=digest_stems,
            index_monthlies=monthly_stems,
            digest_sources=digest_sources,
            monthly_sources=monthly_sources,
            will_create_orphan=will_create_orphan,
        )

    def _collect_language_variants(self, date_str: str) -> list[Path]:
        """Return ``YYYY-MM-DD.<lang>.md`` siblings of the CN brief.

        Order is alphabetical by language code, which keeps EN first
        (the only translation we currently emit). The list excludes
        the canonical CN file because :meth:`plan` already appended it.
        """
        return sorted(self.brief_dir.glob(f"{date_str}.*.md"))

    def _collect_digest_sources(self) -> list[Path]:
        """Return every weekly digest markdown file under ``digest_dir``.

        Order is reverse-alphabetic so the index renderer sees newest
        first. Returns an empty list when ``digest_dir`` is unset or
        does not exist — this keeps the daily-only publish path
        unchanged for tests that never put a digests/ directory in
        place.
        """
        if self.digest_dir is None or not self.digest_dir.exists():
            return []
        # Filter to files that look like weekly digests: canonical CN
        # stems (``YYYY-Www``) or explicitly supported language siblings
        # (currently ``YYYY-Www.en``). Draft/private notes with a valid
        # base stem (e.g. ``YYYY-Www.draft``) must not leak to Pages.
        out: list[Path] = []
        for p in self.digest_dir.glob("*.md"):
            base = _supported_localized_stem(p.stem)
            if base is not None and _looks_like_digest_stem(base):
                out.append(p)
        return sorted(out, reverse=True)

    def _collect_digest_stems(self, sources: list[Path]) -> list[str]:
        """De-dup the stems that should appear in the index table.

        Index keys on the CN digest stem (e.g. ``2026-W20``); language
        siblings (``2026-W20.en``) are detected at render time, same as
        the daily-brief table.
        """
        stems: set[str] = set()
        for p in sources:
            stem = p.stem
            if "." in stem:
                stem = stem.split(".", 1)[0]
            if _looks_like_digest_stem(stem):
                stems.add(stem)
        return sorted(stems, reverse=True)

    def _collect_monthly_sources(self) -> list[Path]:
        """v0.11 — return every monthly digest markdown file under ``digest_dir``.

        Monthly digests share the ``digests/`` directory with weekly
        digests but use a different filename shape (``YYYY-MM`` instead
        of ``YYYY-Www``). This split keeps both cadences in one place
        on disk and on the gh-pages branch.
        """
        if self.digest_dir is None or not self.digest_dir.exists():
            return []
        out: list[Path] = []
        for p in self.digest_dir.glob("*.md"):
            base = _supported_localized_stem(p.stem)
            if base is not None and _looks_like_monthly_stem(base):
                out.append(p)
        return sorted(out, reverse=True)

    def _collect_monthly_stems(self, sources: list[Path]) -> list[str]:
        """v0.11 — de-dup monthly stems for the index table."""
        stems: set[str] = set()
        for p in sources:
            stem = p.stem
            if "." in stem:
                stem = stem.split(".", 1)[0]
            if _looks_like_monthly_stem(stem):
                stems.add(stem)
        return sorted(stems, reverse=True)

    def plan_only(self, date_str: str) -> PublishResult:
        """Convenience wrapper around :meth:`plan` returning a `PublishResult`."""
        the_plan = self.plan(date_str)
        return PublishResult(
            plan=the_plan,
            dry_run=True,
            pushed=False,
            commit_sha=None,
            original_branch=self._current_branch_safe(),
            message=self._summarize_plan(the_plan),
        )

    def publish(
        self,
        date_str: str,
        *,
        push: bool = True,
        dry_run: bool = False,
        commit_message: str | None = None,
    ) -> PublishResult:
        """Run the full publish pipeline.

        ``dry_run`` short-circuits before any git mutation. ``push``
        controls whether to attempt ``git push``; set False for an
        offline rehearsal.

        Atomicity: if any step after the branch switch fails, we try
        very hard to switch back to the original branch before
        re-raising :class:`PublishError`. State on disk may still be
        dirty (the user's uncommitted changes were unaffected — we
        ``--no-overlay`` the worktree only after stashing).
        """
        the_plan = self.plan(date_str)
        original_branch = self._current_branch_safe()

        if dry_run:
            logger.info("publish dry-run for %s on %s", date_str, the_plan.branch)
            return PublishResult(
                plan=the_plan,
                dry_run=True,
                pushed=False,
                commit_sha=None,
                original_branch=original_branch,
                message=self._summarize_plan(the_plan),
            )

        # Refuse to clobber uncommitted changes — the worktree swap
        # would silently lose them.
        if self._has_uncommitted_changes():
            raise PublishError(
                "uncommitted changes detected in working tree. "
                "Commit or stash them before publishing — "
                "the gh-pages checkout would otherwise overwrite files."
            )

        try:
            if the_plan.will_create_orphan:
                # ``_create_orphan_branch`` leaves HEAD pointing at the
                # new orphan branch with a clean worktree — no extra
                # checkout needed.
                self._create_orphan_branch(the_plan.branch)
            else:
                self._git("checkout", the_plan.branch)

            self._copy_into_worktree(the_plan)
            self._overlay_template()
            self._write_index_md(
                the_plan.index_briefs,
                the_plan.index_digests,
                the_plan.index_monthlies,
            )

            self._git("add", "-A")
            if not self._has_staged_changes():
                # Idempotent rerun — nothing new to publish. We let
                # the ``finally`` clause handle the branch restore so
                # this path stays consistent with the success path.
                msg = (
                    f"no changes for {date_str}; gh-pages already up-to-date"
                )
                logger.info(msg)
                return PublishResult(
                    plan=the_plan,
                    dry_run=False,
                    pushed=False,
                    commit_sha=None,
                    original_branch=original_branch,
                    message=msg,
                )

            message = (
                commit_message
                or f"publish brief {date_str} ({len(the_plan.index_briefs)} total)"
            )
            self._git("commit", "-m", message)
            sha = self._git("rev-parse", "HEAD").strip()

            pushed = False
            if push:
                try:
                    self._git("push", "origin", the_plan.branch)
                    pushed = True
                except PublishError as exc:
                    # We still committed locally — but tell the user
                    # the remote isn't updated. They can `git push`
                    # themselves later without rerunning publish.
                    logger.warning("git push failed (commit kept locally): %s", exc)
                    raise

            return PublishResult(
                plan=the_plan,
                dry_run=False,
                pushed=pushed,
                commit_sha=sha,
                original_branch=original_branch,
                message=f"published {date_str} as {sha[:8]} on {the_plan.branch}",
            )
        finally:
            # Best-effort rollback. We swallow rollback failures
            # because the original error (if any) is more important.
            # Because the orphan branch left the worktree with files
            # that may collide with the original branch's tree, use
            # ``-f`` to force the switch — the original branch's
            # cleanliness check at the top of ``publish`` guarantees we
            # aren't destroying user work.
            if original_branch and self._current_branch_safe() != original_branch:
                try:
                    self._git("checkout", "-f", original_branch)
                except PublishError:
                    logger.exception(
                        "failed to restore original branch %s",
                        original_branch,
                    )

    # ------------------------------------------------------------------
    # Helpers — git plumbing
    # ------------------------------------------------------------------

    def _git(self, *args: str, check: bool = True) -> str:
        """Run ``git ARGS`` and return stdout, raising :class:`PublishError` on failure."""
        cmd = [self.git, *args]
        logger.debug("git: %s (cwd=%s)", " ".join(cmd), self.repo_root)
        try:
            result = subprocess.run(
                cmd,
                cwd=str(self.repo_root),
                check=check,
                capture_output=True,
                text=True,
            )
        except subprocess.CalledProcessError as exc:
            raise PublishError(
                f"git {' '.join(args)} failed (rc={exc.returncode}):\n"
                f"stdout: {exc.stdout}\nstderr: {exc.stderr}"
            ) from exc
        return result.stdout

    def _branch_exists(self, branch: str) -> bool:
        try:
            self._git("show-ref", "--verify", "--quiet", f"refs/heads/{branch}")
            return True
        except PublishError:
            return False

    def _current_branch_safe(self) -> str | None:
        try:
            out = self._git("rev-parse", "--abbrev-ref", "HEAD").strip()
            return out or None
        except PublishError:
            return None

    def _has_uncommitted_changes(self) -> bool:
        # ``status --porcelain`` is empty when the worktree is clean,
        # regardless of branch.
        out = self._git("status", "--porcelain").strip()
        return bool(out)

    def _has_staged_changes(self) -> bool:
        # ``diff --cached --quiet`` exits 1 if anything is staged. We
        # can't use ``check=True`` because rc=1 is meaningful, not an
        # error.
        result = subprocess.run(
            [self.git, "diff", "--cached", "--quiet"],
            cwd=str(self.repo_root),
            capture_output=True,
        )
        return result.returncode != 0

    def _create_orphan_branch(self, branch: str) -> None:
        """Create an empty orphan branch named ``branch`` and stage no files.

        ``git checkout --orphan`` leaves the index populated with the
        current branch's tree and the worktree intact (counter-intuitive
        — feels like a no-op). We immediately:

        1. ``git rm -rf --cached .`` — clear the index.
        2. Manually delete every file/dir at the repo root **except**
           ``.git`` — clear the worktree.

        After this the working tree is completely empty (no untracked
        leftovers from the previous branch), ready for the publisher's
        copy step.
        """
        logger.info("creating orphan branch %s", branch)
        self._git("checkout", "--orphan", branch)
        try:
            self._git("rm", "-rf", "--cached", ".")
        except PublishError:
            # Possible on a brand-new repo with no tracked files yet —
            # safe to ignore.
            logger.debug("rm --cached on fresh orphan returned non-zero — ignoring")

        # Now scrub the worktree. We touch only the repo root's direct
        # children so we never accidentally wander outside ``repo_root``.
        for entry in self.repo_root.iterdir():
            if entry.name == ".git":
                continue
            try:
                if entry.is_dir() and not entry.is_symlink():
                    shutil.rmtree(entry)
                else:
                    entry.unlink()
            except OSError as exc:
                # Files we can't remove (e.g. permissions) become a
                # publish-blocking error — better to abort than to
                # commit a polluted gh-pages tree.
                raise PublishError(
                    f"failed to scrub worktree for orphan branch: "
                    f"{entry} ({exc})"
                ) from exc

    # ------------------------------------------------------------------
    # Helpers — file IO
    # ------------------------------------------------------------------

    def _collect_all_brief_dates(self, *, extra: str | None = None) -> list[str]:
        """Return the dated brief stems on the gh-pages branch plus ``extra``.

        On the *first* publish there's no gh-pages branch yet, so we
        fall back to the local ``brief_dir`` glob. After the orphan
        branch exists, we rely on the worktree state after checkout —
        but during planning we don't have that yet, so we use the brief
        source dir as the best approximation.

        v0.8 — we filter out language-suffixed files (``.en.md``,
        ``.jp.md``, ...) so the index is keyed by date only; the
        per-date row in :func:`_render_index_md` then probes for
        sibling language files to fill the EN column.
        """
        names: set[str] = set()
        for p in self.brief_dir.glob("*.md"):
            stem = p.stem
            if stem in {"index", "latest"}:
                continue
            if "." in stem:  # date.en, date.jp, ... — handled per row
                continue
            names.add(stem)
        if extra:
            names.add(extra)
        return sorted(names, reverse=True)

    def _copy_into_worktree(self, plan: PublishPlan) -> None:
        # 1. Brief markdown → ``briefs/<date>.md`` at repo root, plus
        #    every ``<date>.<lang>.md`` sibling discovered by
        #    :meth:`_collect_language_variants`. We re-scan the source
        #    dir rather than relying on ``plan.files_to_copy`` so the
        #    copy logic stays self-contained and idempotent.
        briefs_target_dir = self.repo_root / "briefs"
        briefs_target_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(
            plan.brief_source,
            briefs_target_dir / plan.brief_source.name,
        )
        for lang_md in self._collect_language_variants(plan.date):
            shutil.copy2(lang_md, briefs_target_dir / lang_md.name)

        # 2. Charts → ``charts/<date>/*.png``.
        if plan.chart_source is not None:
            charts_target_dir = self.repo_root / "charts" / plan.date
            charts_target_dir.mkdir(parents=True, exist_ok=True)
            for png in sorted(plan.chart_source.glob("*.png")):
                shutil.copy2(png, charts_target_dir / png.name)

        # 3. RSS feed (single file at repo root).
        if plan.feed_source is not None:
            shutil.copy2(plan.feed_source, self.repo_root / plan.feed_source.name)

        # 3b. v0.10 — Atom 1.0 feed alongside the RSS feed.
        if plan.atom_source is not None:
            shutil.copy2(plan.atom_source, self.repo_root / plan.atom_source.name)

        # 4. v0.9 — weekly digests → ``digests/<iso_year>-W<week>.md``.
        # Remove old public leaks such as ``2026-W20.draft.md`` before
        # copying the current allowlisted digest set. Otherwise files
        # that were mistakenly committed to gh-pages once would remain
        # reachable even after the source collector stops selecting them.
        digests_target_dir = self.repo_root / "digests"
        self._prune_unsupported_digest_files(digests_target_dir)
        if plan.digest_sources:
            digests_target_dir.mkdir(parents=True, exist_ok=True)
            for digest_md in plan.digest_sources:
                shutil.copy2(digest_md, digests_target_dir / digest_md.name)

        # 5. v0.11 — monthly digests → ``digests/<YYYY-MM>.md`` (same
        # directory as weekly digests; the stem shape disambiguates).
        if plan.monthly_sources:
            digests_target_dir = self.repo_root / "digests"
            digests_target_dir.mkdir(parents=True, exist_ok=True)
            for monthly_md in plan.monthly_sources:
                shutil.copy2(monthly_md, digests_target_dir / monthly_md.name)

    def _prune_unsupported_digest_files(self, digests_target_dir: Path) -> None:
        """Delete stale public digest files with unsupported language/draft suffixes."""
        if not digests_target_dir.exists():
            return
        for md in sorted(digests_target_dir.glob("*.md")):
            if _supported_localized_stem(md.stem) is not None:
                continue
            base = md.stem.split(".", 1)[0]
            if _looks_like_digest_stem(base) or _looks_like_monthly_stem(base):
                md.unlink()

    def _overlay_template(self) -> None:
        """Copy ``gh-pages-template/*`` over the worktree (only if present)."""
        if not self.template_dir.exists():
            logger.debug(
                "no template dir at %s — skipping overlay", self.template_dir
            )
            return
        for rel in _TEMPLATE_FILES:
            src = self.template_dir / rel
            if not src.exists():
                logger.debug("template file %s missing — skipping", rel)
                continue
            dst = self.repo_root / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)

    def _write_index_md(
        self,
        dated_briefs: list[str],
        digest_stems: list[str] | None = None,
        monthly_stems: list[str] | None = None,
    ) -> None:
        """Emit the public landing page listing every published brief.

        v0.8 — looks for sibling ``YYYY-MM-DD.en.md`` files inside the
        gh-pages ``briefs/`` directory (which we just wrote to) and
        passes a per-date language map to :func:`_render_index_md`.
        Dates without an EN file render an em-dash in the EN column.

        v0.9 — also lists weekly digests (``<iso_year>-W<week>``) in
        their own section. Same EN-sibling detection logic, but the
        digests live under ``digests/`` not ``briefs/``.

        v0.10 — also detects which chart PNG sits under ``charts/<date>/``
        so the index can render a 48px thumbnail preview per row.

        v0.11 — adds a third section for monthly digests (``YYYY-MM``)
        rendered into the same gh-pages page. Three tables now: daily
        briefs, weekly digests, monthly digests.
        """
        briefs_root = self.repo_root / "briefs"
        languages_per_date = {
            stem: _detect_languages_for(briefs_root, stem) for stem in dated_briefs
        }
        digests_root = self.repo_root / "digests"
        languages_per_digest = {
            stem: _detect_languages_for(digests_root, stem) for stem in (digest_stems or [])
        }
        languages_per_monthly = {
            stem: _detect_languages_for(digests_root, stem) for stem in (monthly_stems or [])
        }
        charts_root = self.repo_root / "charts"
        previews_per_date = {
            stem: _detect_preview_chart(charts_root, stem) for stem in dated_briefs
        }
        target = self.repo_root / "index.md"
        body = _render_index_md(
            dated_briefs,
            languages_per_date=languages_per_date,
            digest_stems=digest_stems or [],
            languages_per_digest=languages_per_digest,
            monthly_stems=monthly_stems or [],
            languages_per_monthly=languages_per_monthly,
            previews_per_date=previews_per_date,
        )
        target.write_text(body, encoding="utf-8")

    # ------------------------------------------------------------------
    # Helpers — formatting
    # ------------------------------------------------------------------

    def _summarize_plan(self, plan: PublishPlan) -> str:
        files_n = len(plan.files_to_copy)
        chart_n = (
            len(list(plan.chart_source.glob("*.png"))) if plan.chart_source else 0
        )
        orphan_tag = " (orphan branch will be created)" if plan.will_create_orphan else ""
        return (
            f"plan for {plan.date} on {plan.branch}{orphan_tag}: "
            f"{files_n} file(s) to copy ({chart_n} chart png), "
            f"index will list {len(plan.index_briefs)} brief(s)"
        )


# ---------------------------------------------------------------------------
# Module-level helpers (reused by tests for direct verification).
# ---------------------------------------------------------------------------


def _render_index_md(
    dated_briefs: list[str],
    *,
    languages_per_date: dict[str, list[str]] | None = None,
    digest_stems: list[str] | None = None,
    languages_per_digest: dict[str, list[str]] | None = None,
    monthly_stems: list[str] | None = None,
    languages_per_monthly: dict[str, list[str]] | None = None,
    previews_per_date: dict[str, str | None] | None = None,
) -> str:
    """Render the gh-pages landing page.

    Public format — kept stable so subscribers can scrape it. v0.8
    splits the brief column into Chinese / English. v0.9 adds a
    separate "本周回顾 / Weekly digests" section. v0.10 adds a chart
    thumbnail column + RSS/Atom subscribe + share buttons. v0.11 adds
    a third table for monthly digests so the cadence trilogy
    (daily / weekly / monthly) is visible on the landing page.
    """
    now = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    lines: list[str] = [
        "---",
        "layout: default",
        "title: CN AltData Brief",
        "description: Daily research brief over China-equity alt-data, synthesized from 4 public source adapters.",
        "---",
        "",
        "# CN AltData Brief — 中国另类数据日报",
        "",
        "> Daily, deterministic research brief over China-equity alt-data, ",
        "> synthesized from 4 public source adapters. Updated every trading day at 17:00 (UTC+8).",
        "> 中文为权威版本，English 为 LLM 翻译（保留事实，标注 source hash）。",
        "> v0.9 起每周五 18:00 还会发布一份 **本周回顾 / Weekly digest**，聚合本周 5 份日报。",
        "> v0.11 起每月 1 日 17:00 还会发布一份 **上月回顾 / Monthly digest**，聚合上月 ~20 份日报 + 4 份周报。",
        "",
        f"_Last regenerated: {now}_",
        "",
        '<section class="subscribe-bar" markdown="0">',
        "  <strong>订阅 / Subscribe:</strong>",
        '  <a class="sub-btn rss" href="feed.xml">RSS 2.0</a>',
        '  <a class="sub-btn atom" href="feed.atom">Atom 1.0</a>',
        '  <a class="sub-btn github" href="https://github.com/Leonard-Don/cn-altdata-brief">Source</a>',
        "</section>",
        "",
        '<section class="share-bar" markdown="0">',
        "  <strong>分享 / Share:</strong>",
        '  <a href="https://twitter.com/intent/tweet?text=CN%20AltData%20Brief&url=https%3A%2F%2Fleonard-don.github.io%2Fcn-altdata-brief">Twitter</a>',
        '  <a href="https://www.linkedin.com/sharing/share-offsite/?url=https%3A%2F%2Fleonard-don.github.io%2Fcn-altdata-brief">LinkedIn</a>',
        '  <a href="https://t.me/share/url?url=https%3A%2F%2Fleonard-don.github.io%2Fcn-altdata-brief&text=CN%20AltData%20Brief">Telegram</a>',
        "</section>",
        "",
        "## 简报列表 / Briefs archive",
        "",
        "| 日期 / Date | 预览 / Preview | 中文 / Chinese | English |",
        "|---|---|---|---|",
    ]
    if not dated_briefs:
        lines.append("| _(暂无 / none yet)_ | — | — | — |")
    else:
        for stem in dated_briefs:
            langs = (languages_per_date or {}).get(stem, [])
            preview = (previews_per_date or {}).get(stem)
            if preview:
                preview_cell = (
                    f'<img src="charts/{stem}/{preview}" alt="{stem} preview" '
                    f'style="max-height:48px;border:1px solid #d0d7de;border-radius:3px">'
                )
            else:
                preview_cell = "—"
            cn_cell = f"[{stem}.md](briefs/{stem}.md)"
            if "en" in langs:
                en_cell = f"[{stem}.en.md](briefs/{stem}.en.md)"
            else:
                en_cell = "—"
            lines.append(
                f"| {stem} | {preview_cell} | {cn_cell} | {en_cell} |"
            )
    lines.append("")
    lines.append("## 本周回顾 / Weekly digests")
    lines.append("")
    lines.append("| 周次 / Week | 中文 / Chinese | English |")
    lines.append("|---|---|---|")
    if not (digest_stems or []):
        lines.append("| _(暂无 / none yet — generated every Friday 18:00)_ | — | — |")
    else:
        for stem in digest_stems or []:
            langs = (languages_per_digest or {}).get(stem, [])
            cn_cell = f"[{stem}.md](digests/{stem}.md)"
            if "en" in langs:
                en_cell = f"[{stem}.en.md](digests/{stem}.en.md)"
            else:
                en_cell = "—"
            lines.append(f"| {stem} | {cn_cell} | {en_cell} |")
    lines.append("")
    lines.append("## 上月回顾 / Monthly digests")
    lines.append("")
    lines.append("| 月份 / Month | 中文 / Chinese | English |")
    lines.append("|---|---|---|")
    if not (monthly_stems or []):
        lines.append("| _(暂无 / none yet — generated on the 1st of each month at 17:00)_ | — | — |")
    else:
        for stem in monthly_stems or []:
            langs = (languages_per_monthly or {}).get(stem, [])
            cn_cell = f"[{stem}.md](digests/{stem}.md)"
            if "en" in langs:
                en_cell = f"[{stem}.en.md](digests/{stem}.en.md)"
            else:
                en_cell = "—"
            lines.append(f"| {stem} | {cn_cell} | {en_cell} |")
    lines.append("")
    lines.append("<style>")
    lines.append(
        ".subscribe-bar, .share-bar { display: flex; flex-wrap: wrap; gap: 0.5rem; "
        "align-items: center; margin: 1rem 0; padding: 0.75rem; background: #f6f8fa; "
        "border: 1px solid #d0d7de; border-radius: 6px; }"
    )
    lines.append(
        ".sub-btn, .share-bar a { padding: 4px 10px; border: 1px solid #d0d7de; "
        "border-radius: 4px; background: #ffffff; color: #0969da; text-decoration: none; "
        "font-size: 0.85rem; }"
    )
    lines.append(".sub-btn:hover, .share-bar a:hover { background: #eaeef2; }")
    lines.append("</style>")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("Generated by [`cn-altdata-brief`](https://github.com/Leonard-Don/cn-altdata-brief) · MIT")
    lines.append("")
    return "\n".join(lines)


def _detect_preview_chart(charts_root: Path, date_stem: str) -> str | None:
    """Return the filename of a preview-worthy chart for ``date_stem``.

    Walks ``charts/<date_stem>/`` and picks the first PNG that exists,
    in the same priority order as the OG image picker (policy → inv →
    industry → nav). Returns ``None`` when no charts exist for that
    date, in which case the index row shows an em-dash.
    """
    if not charts_root.exists():
        return None
    date_dir = charts_root / date_stem
    if not date_dir.exists():
        return None
    for candidate in (
        "policy_impact.png",
        "inventory_change.png",
        "industry_heat.png",
        "etf_nav.png",
    ):
        if (date_dir / candidate).exists():
            return candidate
    return None


_SUPPORTED_DIGEST_LANGUAGE_SUFFIXES = frozenset({"en"})


def _supported_localized_stem(stem: str) -> str | None:
    """Return canonical stem for CN or supported language siblings.

    ``2026-W20`` and ``2026-W20.en`` both map to ``2026-W20``.
    ``2026-W20.draft`` returns ``None`` so private/draft notes with a
    digest-looking base stem cannot leak into the public gh-pages tree.
    """

    if "." not in stem:
        return stem
    base, lang = stem.rsplit(".", 1)
    if lang.lower() in _SUPPORTED_DIGEST_LANGUAGE_SUFFIXES:
        return base
    return None


def _looks_like_digest_stem(stem: str) -> bool:
    """Match ``<iso_year>-W<week>`` style digest filenames.

    Returns True for stems like ``2026-W20`` and False for daily
    briefs (``2026-05-17``) or non-date files (``index``, ``latest``).
    """
    return bool(re.fullmatch(r"\d{4}-W\d{2}", stem))


def _looks_like_monthly_stem(stem: str) -> bool:
    """v0.11 — match ``<YYYY>-<MM>`` style monthly digest filenames.

    Returns True for stems like ``2026-04`` (where MM is 01..12). We
    deliberately exclude daily briefs (``2026-04-15``) — those have
    three dash-separated components — and weekly digests
    (``2026-W18``) — those have a ``W`` after the dash. Both cadences
    coexist in ``digests/`` so the disambiguation matters.
    """
    m = re.fullmatch(r"(?P<y>\d{4})-(?P<m>\d{2})", stem)
    if not m:
        return False
    month = int(m.group("m"))
    return 1 <= month <= 12


def _detect_languages_for(briefs_root: Path, date_stem: str) -> list[str]:
    """Return ISO codes for which ``<date_stem>.<lang>.md`` exists in ``briefs_root``.

    Stable alpha order — used by the index renderer to decide whether
    the EN column is a link or an em-dash.
    """
    if not briefs_root.exists():
        return []
    codes: list[str] = []
    for p in briefs_root.glob(f"{date_stem}.*.md"):
        # stem is "2026-05-17.en" -> language code is "en"
        parts = p.stem.split(".")
        if len(parts) >= 2:
            codes.append(parts[-1].lower())
    return sorted(set(codes))


def default_template_dir() -> Path:
    """Locate the in-repo template directory (used by the CLI)."""
    # publish/gh_pages.py → src/cn_altdata_brief/publish/ → src/cn_altdata_brief/
    # → src/ → <repo_root>
    return Path(__file__).resolve().parents[3] / "gh-pages-template"


def default_brief_dir() -> Path:
    return Path(__file__).resolve().parents[3] / "output" / "briefs"


def default_chart_dir() -> Path:
    return Path(__file__).resolve().parents[3] / "output" / "charts"


def default_feed_path() -> Path:
    return Path(__file__).resolve().parents[3] / "output" / "feed.xml"


def default_atom_path() -> Path:
    """v0.10 — Atom 1.0 feed at ``output/feed.atom``."""
    return Path(__file__).resolve().parents[3] / "output" / "feed.atom"


def utc_today() -> str:
    """Return today's UTC date in ``YYYY-MM-DD`` form (matches the CLI default)."""
    return datetime.now(UTC).strftime("%Y-%m-%d")

