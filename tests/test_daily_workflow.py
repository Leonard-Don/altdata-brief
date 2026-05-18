"""Contract checks for the scheduled/manual Daily Brief workflow."""

from __future__ import annotations

from pathlib import Path

WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "daily.yml"
GITIGNORE = Path(__file__).resolve().parents[1] / ".gitignore"


def _workflow_text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def test_workflow_dispatch_exposes_publish_input() -> None:
    """Manual runs must expose an explicit publish toggle, defaulting off."""
    text = _workflow_text()

    assert "workflow_dispatch:\n    inputs:\n      publish:" in text
    assert "description:" in text
    assert "type: boolean" in text
    assert "default: false" in text


def test_daily_workflow_uses_cli_publisher_contract() -> None:
    """The workflow must reuse the CLI publisher so all v0.11 artifacts ship."""
    text = _workflow_text()

    assert "uv run cn-altdata-brief publish" in text
    assert "--date \"$DATE\"" in text
    assert "--dry-run" in text
    assert "inputs.publish == true" in text
    # Guard against the legacy hand-copy path, which missed Atom and digest artifacts.
    assert "cp output/briefs/${DATE}.md briefs/" not in text
    assert "cp output/feed.xml feed.xml" not in text


def test_actions_source_checkouts_are_ignored_before_cli_publish() -> None:
    """Actions checks out upstream repos under sources/, which publish must ignore."""
    workflow_text = _workflow_text()
    gitignore_text = f"\n{GITIGNORE.read_text(encoding='utf-8')}\n"

    assert "path: sources/" in workflow_text
    assert "\nsources/\n" in gitignore_text
