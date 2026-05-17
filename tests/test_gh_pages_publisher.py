"""v0.6 — tests for the GhPagesPublisher.

These tests build throwaway git repos under ``tmp_path`` and exercise
the publisher's real subprocess git invocations. We deliberately avoid
mocking subprocess so we get the same failure surface as production —
empty stdin, branch creation/checkout semantics, etc.

Each test runs ``git init`` with an explicit ``-b main`` and pre-commits
a single file so the publisher has a coherent ``HEAD`` to roll back to.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from cn_altdata_brief.publish.gh_pages import (
    GhPagesPublisher,
    PublishError,
    _render_index_md,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        text=True,
    ).stdout


@pytest.fixture
def tmp_repo(tmp_path: Path) -> Path:
    """Fresh git repo on ``main`` with one tracked file."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "test")
    # Disable signing so signed-commit environments don't break the test.
    _git(repo, "config", "commit.gpgsign", "false")
    (repo / "README.md").write_text("hello\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "initial")
    return repo


@pytest.fixture
def brief_layout(tmp_path: Path) -> dict[str, Path]:
    """Generate a realistic ``output/`` layout with one dated brief + charts."""
    out = tmp_path / "output"
    briefs = out / "briefs"
    charts = out / "charts" / "2026-05-17"
    briefs.mkdir(parents=True)
    charts.mkdir(parents=True)
    (briefs / "2026-05-17.md").write_text(
        "# Brief 2026-05-17\n\nbody\n", encoding="utf-8"
    )
    # The publisher should silently ignore index.md / latest.md as
    # dated briefs (they aren't dates).
    (briefs / "index.md").write_text("# index placeholder\n", encoding="utf-8")
    (briefs / "latest.md").write_text("# latest placeholder\n", encoding="utf-8")
    (charts / "policy_impact.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    (charts / "etf_nav.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    feed = out / "feed.xml"
    feed.write_text("<rss/>\n", encoding="utf-8")
    return {"briefs": briefs, "charts": out / "charts", "feed": feed}


@pytest.fixture
def template_dir(tmp_path: Path) -> Path:
    tdir = tmp_path / "gh-pages-template"
    (tdir / "_layouts").mkdir(parents=True)
    (tdir / "_config.yml").write_text("title: Test Site\n", encoding="utf-8")
    (tdir / "_layouts" / "brief.html").write_text(
        "<html>{{ content }}</html>\n", encoding="utf-8"
    )
    return tdir


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_constructor_stores_paths(tmp_repo: Path, brief_layout: dict[str, Path]) -> None:
    pub = GhPagesPublisher(
        brief_dir=brief_layout["briefs"],
        chart_dir=brief_layout["charts"],
        feed_path=brief_layout["feed"],
        repo_root=tmp_repo,
        gh_pages_branch="gh-pages",
    )
    assert pub.brief_dir == brief_layout["briefs"]
    assert pub.chart_dir == brief_layout["charts"]
    assert pub.feed_path == brief_layout["feed"]
    assert pub.repo_root == tmp_repo
    assert pub.gh_pages_branch == "gh-pages"


def test_publish_missing_brief_raises(
    tmp_repo: Path, brief_layout: dict[str, Path]
) -> None:
    pub = GhPagesPublisher(
        brief_dir=brief_layout["briefs"],
        chart_dir=brief_layout["charts"],
        feed_path=brief_layout["feed"],
        repo_root=tmp_repo,
    )
    with pytest.raises(PublishError, match="brief not found"):
        pub.publish("2099-12-31", push=False)


def test_dry_run_lists_files_and_skips_git(
    tmp_repo: Path, brief_layout: dict[str, Path]
) -> None:
    pub = GhPagesPublisher(
        brief_dir=brief_layout["briefs"],
        chart_dir=brief_layout["charts"],
        feed_path=brief_layout["feed"],
        repo_root=tmp_repo,
    )
    result = pub.publish("2026-05-17", push=False, dry_run=True)
    # Brief + 2 charts + feed.xml = 4 files.
    paths = {p.name for p in result.plan.files_to_copy}
    assert paths == {"2026-05-17.md", "policy_impact.png", "etf_nav.png", "feed.xml"}
    assert result.dry_run is True
    assert result.commit_sha is None
    assert result.pushed is False
    # gh-pages branch must NOT exist yet — we didn't touch git.
    assert not (tmp_repo / ".git" / "refs" / "heads" / "gh-pages").exists()
    # We must still be on main.
    head = _git(tmp_repo, "rev-parse", "--abbrev-ref", "HEAD").strip()
    assert head == "main"


def test_publish_creates_orphan_branch_and_commits(
    tmp_repo: Path,
    brief_layout: dict[str, Path],
    template_dir: Path,
) -> None:
    pub = GhPagesPublisher(
        brief_dir=brief_layout["briefs"],
        chart_dir=brief_layout["charts"],
        feed_path=brief_layout["feed"],
        template_dir=template_dir,
        repo_root=tmp_repo,
    )
    result = pub.publish("2026-05-17", push=False)
    assert result.dry_run is False
    assert result.pushed is False  # no remote configured
    assert result.commit_sha is not None
    # Must be back on main.
    assert _git(tmp_repo, "rev-parse", "--abbrev-ref", "HEAD").strip() == "main"
    # gh-pages branch exists.
    branches = _git(tmp_repo, "branch", "--list", "gh-pages").strip()
    assert "gh-pages" in branches
    # Inspect the tree of the gh-pages tip — index.md, _config.yml,
    # brief, charts, feed must be present.
    tree = _git(tmp_repo, "ls-tree", "-r", "--name-only", "gh-pages")
    assert "index.md" in tree
    assert "_config.yml" in tree
    assert "_layouts/brief.html" in tree
    assert "briefs/2026-05-17.md" in tree
    assert "charts/2026-05-17/policy_impact.png" in tree
    assert "feed.xml" in tree


def test_publish_copies_atom_feed_alongside_rss(
    tmp_repo: Path,
    brief_layout: dict[str, Path],
    template_dir: Path,
) -> None:
    """v0.10 — Atom feed is planned and copied onto the gh-pages branch."""
    rss_xml = "<rss><channel><title>rss fixture</title></channel></rss>\n"
    atom_xml = '<feed xmlns="http://www.w3.org/2005/Atom"><title>atom fixture</title></feed>\n'
    brief_layout["feed"].write_text(rss_xml, encoding="utf-8")
    atom = brief_layout["feed"].with_name("feed.atom")
    atom.write_text(atom_xml, encoding="utf-8")
    pub = GhPagesPublisher(
        brief_dir=brief_layout["briefs"],
        chart_dir=brief_layout["charts"],
        feed_path=brief_layout["feed"],
        atom_path=atom,
        template_dir=template_dir,
        repo_root=tmp_repo,
    )

    dry_run = pub.publish("2026-05-17", push=False, dry_run=True)
    assert dry_run.plan.feed_source == brief_layout["feed"]
    assert dry_run.plan.atom_source == atom
    assert {p.name for p in dry_run.plan.files_to_copy} == {
        "2026-05-17.md",
        "policy_impact.png",
        "etf_nav.png",
        "feed.xml",
        "feed.atom",
    }

    result = pub.publish("2026-05-17", push=False)
    assert result.commit_sha is not None
    tree_entries = set(_git(tmp_repo, "ls-tree", "-r", "--name-only", "gh-pages").splitlines())
    assert "feed.xml" in tree_entries
    assert "feed.atom" in tree_entries
    assert _git(tmp_repo, "show", "gh-pages:feed.xml") == rss_xml
    assert _git(tmp_repo, "show", "gh-pages:feed.atom") == atom_xml


def test_index_md_renders_5_row_table() -> None:
    body = _render_index_md(
        ["2026-05-17", "2026-05-16", "2026-05-15", "2026-05-14", "2026-05-13"]
    )
    # YAML front matter
    assert body.startswith("---\nlayout: default")
    # All 5 dates appear as table rows
    rows = [
        line
        for line in body.splitlines()
        if line.startswith("| 2026-05-")
    ]
    assert len(rows) == 5
    # Newest first
    assert rows[0].startswith("| 2026-05-17")
    assert rows[-1].startswith("| 2026-05-13")
    # Each row links into briefs/
    for r in rows:
        assert "briefs/" in r and ".md)" in r


def test_index_md_empty_archive_has_placeholder() -> None:
    body = _render_index_md([])
    assert "(暂无 / none yet)" in body


def test_rollback_returns_to_original_branch_on_failure(
    tmp_repo: Path,
    brief_layout: dict[str, Path],
    template_dir: Path,
) -> None:
    """Simulate a commit failure → publisher must restore the original branch."""
    pub = GhPagesPublisher(
        brief_dir=brief_layout["briefs"],
        chart_dir=brief_layout["charts"],
        feed_path=brief_layout["feed"],
        template_dir=template_dir,
        repo_root=tmp_repo,
    )
    # Sabotage the commit step by installing a pre-commit hook that
    # always exits 1. Robust across git versions and ignores user.email
    # inheritance from $HOME. Hook permissions must include +x.
    hooks_dir = tmp_repo / ".git" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    hook = hooks_dir / "pre-commit"
    hook.write_text("#!/usr/bin/env bash\necho 'sabotage' >&2\nexit 1\n", encoding="utf-8")
    hook.chmod(0o755)

    with pytest.raises(PublishError):
        pub.publish("2026-05-17", push=False)
    # After failure we must still be on main (rollback ran).
    head = _git(tmp_repo, "rev-parse", "--abbrev-ref", "HEAD").strip()
    assert head == "main"


def test_publish_refuses_when_worktree_dirty(
    tmp_repo: Path, brief_layout: dict[str, Path]
) -> None:
    # Stage a change in the consumer repo. The publisher must refuse to
    # proceed — checking out gh-pages would clobber it.
    (tmp_repo / "README.md").write_text("modified!\n", encoding="utf-8")
    pub = GhPagesPublisher(
        brief_dir=brief_layout["briefs"],
        chart_dir=brief_layout["charts"],
        feed_path=brief_layout["feed"],
        repo_root=tmp_repo,
    )
    with pytest.raises(PublishError, match="uncommitted changes"):
        pub.publish("2026-05-17", push=False)


def test_idempotent_rerun_with_no_changes(
    tmp_repo: Path,
    brief_layout: dict[str, Path],
    template_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When nothing has changed between runs, the second publish must be a no-op.

    The ``_render_index_md`` helper embeds a "Last regenerated" UTC
    timestamp, which would normally guarantee every rerun produces a
    diff. Freeze the timestamp via monkeypatch so we exercise the
    real "no staged changes → skip commit" path.
    """
    from cn_altdata_brief.publish import gh_pages as mod

    frozen = "2026-05-17 08:00 UTC"

    class _FrozenDatetime:
        @classmethod
        def now(cls, tz=None):
            from datetime import datetime as _dt

            return _dt(2026, 5, 17, 8, 0, 0, tzinfo=tz)

    monkeypatch.setattr(mod, "datetime", _FrozenDatetime)
    _ = frozen  # silence flake

    pub = GhPagesPublisher(
        brief_dir=brief_layout["briefs"],
        chart_dir=brief_layout["charts"],
        feed_path=brief_layout["feed"],
        template_dir=template_dir,
        repo_root=tmp_repo,
    )
    first = pub.publish("2026-05-17", push=False)
    assert first.commit_sha is not None
    sha_first = _git(tmp_repo, "rev-parse", "gh-pages").strip()

    # Second publish with frozen clock → identical index.md, no diff,
    # no commit. The publisher should return a "no changes" result.
    second = pub.publish("2026-05-17", push=False)
    assert second.commit_sha is None
    assert "no changes" in second.message
    sha_second = _git(tmp_repo, "rev-parse", "gh-pages").strip()
    assert sha_first == sha_second
    # Branch must still be main after the no-op path.
    assert _git(tmp_repo, "rev-parse", "--abbrev-ref", "HEAD").strip() == "main"


def test_subprocess_git_invocations_use_repo_root(
    tmp_repo: Path,
    brief_layout: dict[str, Path],
    template_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The publisher must run git with cwd=repo_root, not the caller's cwd."""
    pub = GhPagesPublisher(
        brief_dir=brief_layout["briefs"],
        chart_dir=brief_layout["charts"],
        feed_path=brief_layout["feed"],
        template_dir=template_dir,
        repo_root=tmp_repo,
    )

    captured_cwds: list[str] = []
    real_run = subprocess.run

    def recording_run(cmd, *args, **kwargs):
        if cmd and cmd[0] == "git":
            captured_cwds.append(str(kwargs.get("cwd")))
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", recording_run)
    result = pub.publish("2026-05-17", push=False)
    assert result.commit_sha is not None
    # Every git invocation must have been against the tmp_repo.
    assert captured_cwds, "no git subprocess captured"
    assert all(cwd == str(tmp_repo) for cwd in captured_cwds), captured_cwds


def test_chart_dir_missing_silently_skipped(
    tmp_repo: Path, brief_layout: dict[str, Path], template_dir: Path
) -> None:
    # Remove the chart subdir entirely. The publisher should still
    # publish the brief + feed without crashing.
    shutil.rmtree(brief_layout["charts"] / "2026-05-17")
    pub = GhPagesPublisher(
        brief_dir=brief_layout["briefs"],
        chart_dir=brief_layout["charts"],
        feed_path=brief_layout["feed"],
        template_dir=template_dir,
        repo_root=tmp_repo,
    )
    result = pub.publish("2026-05-17", push=False)
    assert result.commit_sha is not None
    tree = _git(tmp_repo, "ls-tree", "-r", "--name-only", "gh-pages")
    assert "briefs/2026-05-17.md" in tree
    assert "charts/" not in tree  # no chart was copied


def test_v09_digests_published_alongside_briefs(
    tmp_repo: Path,
    brief_layout: dict[str, Path],
    template_dir: Path,
    tmp_path: Path,
) -> None:
    """v0.9 — weekly digests in digest_dir land on the gh-pages branch."""
    digests = tmp_path / "output" / "digests"
    digests.mkdir(parents=True, exist_ok=True)
    (digests / "2026-W20.md").write_text(
        "# 本周回顾 W20 — 2026-05-11 → 2026-05-15\nbody\n",
        encoding="utf-8",
    )
    pub = GhPagesPublisher(
        brief_dir=brief_layout["briefs"],
        chart_dir=brief_layout["charts"],
        feed_path=brief_layout["feed"],
        template_dir=template_dir,
        repo_root=tmp_repo,
        digest_dir=digests,
    )
    result = pub.publish("2026-05-17", push=False)
    assert result.commit_sha is not None
    tree = _git(tmp_repo, "ls-tree", "-r", "--name-only", "gh-pages")
    assert "digests/2026-W20.md" in tree
    # Index must list the digest in its dedicated section.
    index = subprocess.run(
        ["git", "show", "gh-pages:index.md"],
        cwd=str(tmp_repo),
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "本周回顾 / Weekly digests" in index
    assert "2026-W20" in index
