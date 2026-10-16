"""tm teaches codegraph as a CLI: nothing it ships mentions the MCP server's tools or wiring."""

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
MCP_STRINGS = [
    "codegraph_explore",
    "mcp__codegraph",
    "projectPath",
    "serve --mcp",
    "codegraph install",
]
SHIPPED = [
    *sorted((ROOT / "src" / "taskmanager" / "guides").glob("*.md")),
    *sorted(p for p in (ROOT / "plugin").rglob("*") if p.is_file()),
    ROOT / "README.md",
    ROOT / "GEMINI.md",
]
USAGE_LINE = (
    "With `codegraph` on PATH, agents take its usage from `tm guide <role>` "
    "(or `tm guide codegraph`), and a brief forwards the claim's `codegraph:` lines."
)
INIT_RULE = "run `codegraph init -y <repo>` first, then ask the index"


def _read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def test_the_shipped_files_are_found() -> None:
    names = {p.name for p in SHIPPED}

    assert {"codegraph.md", "implement.md", "SKILL.md", "tm-wave.js", "README.md"} <= names


@pytest.mark.parametrize("path", SHIPPED, ids=lambda p: str(p.relative_to(ROOT)))
def test_no_shipped_guide_or_plugin_file_names_a_codegraph_mcp_tool_or_its_wiring(
    path: Path,
) -> None:
    text = path.read_text(encoding="utf-8")

    assert [s for s in MCP_STRINGS if s in text] == []


@pytest.mark.parametrize("skill", ["dispatcher", "taskmanager"])
def test_the_dispatcher_and_taskmanager_skills_point_agents_to_the_codegraph_guide(
    skill: str,
) -> None:
    assert USAGE_LINE + "\n" in _read(f"plugin/skills/{skill}/SKILL.md")


@pytest.mark.parametrize("skill", ["init", "design"])
def test_the_init_and_design_skills_init_an_unindexed_repo_before_asking_the_index(
    skill: str,
) -> None:
    text = _read(f"plugin/skills/{skill}/SKILL.md")

    assert "(`codegraph status <repo>` prints `Not initialized`)" in text
    assert INIT_RULE in text


def test_the_readme_says_codegraph_is_used_as_a_cli_only() -> None:
    readme = _read("README.md")

    assert "tm uses codegraph as a CLI only" in readme
    assert "it never wires codegraph's MCP server into an agent" in readme
    assert "indexes a repository on its first implement or fix claim" in readme
