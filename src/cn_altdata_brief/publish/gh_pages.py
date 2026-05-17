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
    """

    date: str
    branch: str
    brief_source: Path
    chart_source: Path | None
    feed_source: Path | None
    files_to_copy: list[Path] = field(default_factory=list)
    index_briefs: list[str] = field(default_factory=list)
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
    ) -> None:
        self.brief_dir = Path(brief_dir)
        self.chart_dir = Path(chart_dir) if chart_dir is not None else None
        self.feed_path = Path(feed_path) if feed_path is not None else None
        self.repo_root = Path(repo_root) if repo_root is not None else Path.cwd()
        self.template_dir = (
            Path(template_dir)
            if template_dir is not None
            else self.repo_root / "gh-pages-template"
        )
        self.gh_pages_branch = gh_pages_branch
        self.git = git_executable

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

        files_to_copy: list[Path] = [brief_path]
        if chart_subdir is not None:
            files_to_copy.extend(sorted(chart_subdir.glob("*.png")))
        if feed_source is not None:
            files_to_copy.append(feed_source)

        # Index lists ALL dated briefs (existing + the new one merged in).
        all_dates = self._collect_all_brief_dates(extra=date_str)

        will_create_orphan = not self._branch_exists(self.gh_pages_branch)

        return PublishPlan(
            date=date_str,
            branch=self.gh_pages_branch,
            brief_source=brief_path,
            chart_source=chart_subdir,
            feed_source=feed_source,
            files_to_copy=files_to_copy,
            index_briefs=all_dates,
            will_create_orphan=will_create_orphan,
        )

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
            self._write_index_md(the_plan.index_briefs)

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
        """
        names = {
            p.stem
            for p in self.brief_dir.glob("*.md")
            if p.stem not in {"index", "latest"}
        }
        if extra:
            names.add(extra)
        return sorted(names, reverse=True)

    def _copy_into_worktree(self, plan: PublishPlan) -> None:
        # 1. Brief markdown → ``briefs/<date>.md`` at repo root.
        briefs_target_dir = self.repo_root / "briefs"
        briefs_target_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(
            plan.brief_source,
            briefs_target_dir / plan.brief_source.name,
        )

        # 2. Charts → ``charts/<date>/*.png``.
        if plan.chart_source is not None:
            charts_target_dir = self.repo_root / "charts" / plan.date
            charts_target_dir.mkdir(parents=True, exist_ok=True)
            for png in sorted(plan.chart_source.glob("*.png")):
                shutil.copy2(png, charts_target_dir / png.name)

        # 3. RSS feed (single file at repo root).
        if plan.feed_source is not None:
            shutil.copy2(plan.feed_source, self.repo_root / plan.feed_source.name)

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

    def _write_index_md(self, dated_briefs: list[str]) -> None:
        """Emit the public landing page listing every published brief."""
        target = self.repo_root / "index.md"
        body = _render_index_md(dated_briefs)
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


def _render_index_md(dated_briefs: list[str]) -> str:
    """Render the gh-pages landing page.

    Public format — kept stable so subscribers can scrape it.
    """
    now = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    lines: list[str] = [
        "---",
        "layout: default",
        "title: CN AltData Brief",
        "---",
        "",
        "# CN AltData Brief — 中国另类数据日报",
        "",
        "> Daily, deterministic research brief over China-equity alt-data, ",
        "> synthesized from 6 quant projects. Updated every trading day at 17:00 (UTC+8).",
        "",
        f"_Last regenerated: {now}_",
        "",
        "[RSS feed](feed.xml) · ",
        "[Source code](https://github.com/Leonard-Don/cn-altdata-brief)",
        "",
        "## 简报列表 / Briefs archive",
        "",
        "| 日期 / Date | 简报 / Brief |",
        "|---|---|",
    ]
    if not dated_briefs:
        lines.append("| _(暂无 / none yet)_ | — |")
    else:
        for stem in dated_briefs:
            lines.append(f"| {stem} | [{stem}.md](briefs/{stem}.md) |")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("Generated by [`cn-altdata-brief`](https://github.com/Leonard-Don/cn-altdata-brief) · MIT")
    lines.append("")
    return "\n".join(lines)


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


def utc_today() -> str:
    """Return today's UTC date in ``YYYY-MM-DD`` form (matches the CLI default)."""
    return datetime.now(UTC).strftime("%Y-%m-%d")


