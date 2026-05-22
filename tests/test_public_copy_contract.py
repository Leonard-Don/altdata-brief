"""Public copy consistency checks for the v0.11 four-source surface."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PUBLIC_COPY_PATHS = (
    ROOT / "README.md",
    ROOT / "pyproject.toml",
    ROOT / "docs",
    ROOT / "src",
    ROOT / "templates",
    ROOT / ".github" / "workflows",
)
STALE_COPY_NEEDLES = (
    "6 quant projects",
    "portfolio of 6",
    "synthesized from 6",
    "6 个项目",
    "6 个量化项目",
    "4 个本地量化项目",
    "真实数据缓存",
)


def _iter_public_copy_files() -> list[Path]:
    files: list[Path] = []
    for path in PUBLIC_COPY_PATHS:
        if path.is_file():
            files.append(path)
            continue
        files.extend(
            p
            for p in path.rglob("*")
            if p.is_file() and p.suffix in {".md", ".py", ".j2", ".toml", ".yml"}
        )
    return sorted(files)


def test_public_copy_has_no_stale_six_project_surface() -> None:
    """Reader-visible metadata should consistently describe the 4-source surface."""
    hits: list[str] = []
    for path in _iter_public_copy_files():
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), start=1):
            if any(needle in line for needle in STALE_COPY_NEEDLES):
                hits.append(f"{path.relative_to(ROOT)}:{lineno}: {line}")

    assert hits == []


def test_readme_has_no_commercial_positioning() -> None:
    """README should present the project as non-commercial research tooling."""
    forbidden = (
        "商业化",
        "Monetization",
        "付费",
        "Paid",
        "Substack",
        "外包",
        "Boss/Upwork",
        "¥",
        "content-as-distribution",
    )
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    hits = [needle for needle in forbidden if needle in text]

    assert hits == []
