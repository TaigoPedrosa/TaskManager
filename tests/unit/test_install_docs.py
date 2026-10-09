from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
INSTRUCTIONS = [
    "README.md",
    "CONTRIBUTING.md",
    "GEMINI.md",
    "plugin/skills/taskmanager/SKILL.md",
    "plugin/skills/dispatcher/SKILL.md",
    "src/taskmanager/guides/overview.md",
]
# A pinned tool install bypasses install.sh, and a `v<version>` tag does not exist before 1.0.0.
FORBIDDEN = ["uv tool install git+", "@v<", "@v{", "TaskManager@v"]


def _scanned() -> list[Path]:
    files = [p for d in ("plugin", "src/taskmanager") for p in (REPO / d).rglob("*") if p.is_file()]
    return [*files, REPO / "README.md", REPO / "GEMINI.md"]


@pytest.mark.parametrize("path", INSTRUCTIONS)
def test_every_install_instruction_points_at_install_sh(path: str) -> None:
    assert "install.sh" in (REPO / path).read_text(encoding="utf-8")


def test_no_shipped_file_installs_from_a_version_tag() -> None:
    offending = [
        f"{p.relative_to(REPO)}: {needle}"
        for p in _scanned()
        if p.suffix in {".md", ".py", ".json", ".js", ".mjs", ".html", ".yaml", ".yml", ".toml"}
        for needle in FORBIDDEN
        if needle in p.read_text(encoding="utf-8", errors="replace")
    ]
    assert offending == []


def test_readme_install_section_is_install_sh_alone() -> None:
    text = (REPO / "README.md").read_text(encoding="utf-8")
    section = text.split("## Install\n", 1)[1].split("\n## ", 1)[0]
    for command in ("install.sh | bash", "./install.sh status", "./install.sh uninstall", "--from"):
        assert command in section
    assert "claude plugin marketplace add" not in section
    assert "claude plugin install" not in section
