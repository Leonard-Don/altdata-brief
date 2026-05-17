"""v0.5 — launchd installer tests.

These tests do NOT actually call ``launchctl load`` — they validate the
plist template, the install/uninstall shell paths, and the run_now
wrapper structure. The real ``launchctl load`` step is exercised
manually by the user when they run the installer.
"""

from __future__ import annotations

import os
import re
import stat
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = PROJECT_ROOT / "scripts"
INSTALL_SH = SCRIPTS / "install_launchd_macos.sh"
UNINSTALL_SH = SCRIPTS / "uninstall_launchd_macos.sh"
RUN_NOW_SH = SCRIPTS / "run_now.sh"


def _emit_plist(target_dir: Path) -> Path:
    """Run the installer's plist-emit logic only.

    We can't safely call the real ``install_launchd_macos.sh`` because
    it also invokes ``launchctl load`` on the user's box. Instead, we
    pull the heredoc-emitted plist template out of the installer
    script and render it with a fake HOME so the real LaunchAgents
    dir is untouched.
    """
    env = os.environ.copy()
    env["HOME"] = str(target_dir)
    # Stop the script before it calls launchctl, by overriding the
    # `launchctl` command on PATH with a no-op.
    fake_bin = target_dir / "fake-bin"
    fake_bin.mkdir(parents=True, exist_ok=True)
    fake_launchctl = fake_bin / "launchctl"
    fake_launchctl.write_text(
        "#!/usr/bin/env bash\n"
        "# test stub — print args and exit 0, except for 'list' which\n"
        "# returns nothing (so the installer treats the job as new).\n"
        'if [ "${1:-}" = "list" ]; then exit 0; fi\n'
        "echo \"[fake-launchctl] $@\" >&2\n"
        "exit 0\n",
        encoding="utf-8",
    )
    fake_launchctl.chmod(0o755)
    env["PATH"] = f"{fake_bin}:{env.get('PATH', '')}"

    result = subprocess.run(
        ["/bin/bash", str(INSTALL_SH)],
        env=env,
        capture_output=True,
        text=True,
        cwd=str(PROJECT_ROOT),
    )
    # We tolerate exit code 0 or 3 (3 = launchctl list didn't show label,
    # which is expected with a no-op fake). Anything else is a real bug.
    assert result.returncode in (0, 3), (
        f"installer exited unexpectedly with {result.returncode}\n"
        f"stdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}\n"
    )
    plist = target_dir / "Library" / "LaunchAgents" / "com.leonardodon.cn-altdata-brief.plist"
    assert plist.exists(), f"installer did not produce plist at {plist}"
    return plist


@pytest.mark.skipif(
    os.uname().sysname != "Darwin",
    reason="launchd is a macOS-only subsystem",
)
def test_plist_template_renders_valid_xml(tmp_path: Path) -> None:
    plist = _emit_plist(tmp_path)
    # Parse as XML — DOCTYPE is fine, ET handles it.
    tree = ET.parse(plist)
    root = tree.getroot()
    assert root.tag == "plist"
    # Every plist must have one dict child holding the keys.
    top_dicts = root.findall("dict")
    assert len(top_dicts) == 1
    body = top_dicts[0]
    keys = [c.text for c in body.findall("key")]
    # Must declare the core keys launchd expects.
    for required in (
        "Label",
        "ProgramArguments",
        "WorkingDirectory",
        "StartCalendarInterval",
        "StandardOutPath",
        "StandardErrorPath",
    ):
        assert required in keys, f"plist missing required key {required}"


@pytest.mark.skipif(
    os.uname().sysname != "Darwin",
    reason="launchd is a macOS-only subsystem",
)
def test_schedule_is_weekdays_at_17_00(tmp_path: Path) -> None:
    plist = _emit_plist(tmp_path)
    tree = ET.parse(plist)
    root = tree.getroot()
    body = root.find("dict")
    assert body is not None

    # Find the <array> right after the <key>StartCalendarInterval</key>.
    children = list(body)
    schedule_array = None
    for idx, child in enumerate(children):
        if child.tag == "key" and child.text == "StartCalendarInterval":
            schedule_array = children[idx + 1]
            break
    assert schedule_array is not None, "StartCalendarInterval key missing"
    assert schedule_array.tag == "array"

    entries = schedule_array.findall("dict")
    assert len(entries) == 5, "should schedule for 5 weekdays"

    weekdays_seen = set()
    for entry in entries:
        kv: dict[str, str] = {}
        for k, v in zip(entry.findall("key"), entry.findall("integer"), strict=True):
            assert k.text is not None
            assert v.text is not None
            kv[k.text] = v.text
        assert kv["Hour"] == "17", f"expected 17:00, got Hour={kv['Hour']}"
        assert kv["Minute"] == "0", f"expected 17:00, got Minute={kv['Minute']}"
        weekdays_seen.add(int(kv["Weekday"]))

    # launchd weekday 1..5 = Mon..Fri
    assert weekdays_seen == {1, 2, 3, 4, 5}


@pytest.mark.skipif(
    os.uname().sysname != "Darwin",
    reason="launchd is a macOS-only subsystem",
)
def test_install_creates_plist_at_expected_path(tmp_path: Path) -> None:
    plist = _emit_plist(tmp_path)
    expected = (
        tmp_path / "Library" / "LaunchAgents" / "com.leonardodon.cn-altdata-brief.plist"
    )
    assert plist == expected
    text = plist.read_text(encoding="utf-8")
    # ProgramArguments must invoke run_now.sh from the real project root.
    assert "scripts/run_now.sh" in text
    # WorkingDirectory must be the actual project root, not literal $PROJECT_ROOT.
    assert str(PROJECT_ROOT) in text
    assert "$PROJECT_ROOT" not in text


@pytest.mark.skipif(
    os.uname().sysname != "Darwin",
    reason="launchd is a macOS-only subsystem",
)
def test_uninstall_removes_plist(tmp_path: Path) -> None:
    plist = _emit_plist(tmp_path)
    assert plist.exists()

    env = os.environ.copy()
    env["HOME"] = str(tmp_path)
    fake_bin = tmp_path / "fake-bin"
    # Reuse the fake-launchctl created by _emit_plist, then run uninstall.
    env["PATH"] = f"{fake_bin}:{env.get('PATH', '')}"

    result = subprocess.run(
        ["/bin/bash", str(UNINSTALL_SH)],
        env=env,
        capture_output=True,
        text=True,
        cwd=str(PROJECT_ROOT),
    )
    assert result.returncode == 0, (
        f"uninstall failed: stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert not plist.exists(), "uninstall should have removed the plist"


def test_run_now_has_correct_shebang_and_uv_invocation() -> None:
    """run_now.sh must be a valid bash script that calls `uv run`."""
    assert RUN_NOW_SH.exists()
    # Executable bit must be set so launchd can invoke it directly.
    mode = RUN_NOW_SH.stat().st_mode
    assert mode & stat.S_IXUSR, "run_now.sh must be executable"
    text = RUN_NOW_SH.read_text(encoding="utf-8")
    # POSIX shebang on first line.
    first = text.splitlines()[0]
    assert first == "#!/usr/bin/env bash", f"unexpected shebang: {first!r}"
    # Must invoke `uv run cn-altdata-brief generate` with --source-mode auto.
    assert re.search(r"uv run cn-altdata-brief generate --source-mode auto", text), (
        "run_now.sh should call `uv run cn-altdata-brief generate --source-mode auto`"
    )
    # Must wire up the macOS notification fallback on failure.
    assert "osascript" in text
