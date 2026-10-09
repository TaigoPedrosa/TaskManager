import json
from importlib.resources import files
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
INSTALL = (
    "curl -fsSL https://raw.githubusercontent.com/TaigoPedrosa/TaskManager/main/install.sh | bash"
)
PLUGIN_PARTS = {".claude-plugin", "agents", "commands", "skills", "workflows"}


def _plugin_root() -> Path:
    marketplace = json.loads((REPO / ".claude-plugin/marketplace.json").read_text(encoding="utf-8"))
    (entry,) = marketplace["plugins"]
    return (REPO / entry["source"]).resolve()


def test_the_marketplace_installs_only_the_plugin_s_own_parts() -> None:
    root = _plugin_root()
    assert root != REPO
    assert {p.name for p in root.iterdir()} == PLUGIN_PARTS
    manifest = json.loads((root / ".claude-plugin/plugin.json").read_text(encoding="utf-8"))
    assert manifest["name"] == "taskmanager"


def test_the_package_carries_no_second_copy_of_the_skills() -> None:
    assert not files("taskmanager").joinpath("skills").is_dir()


@pytest.mark.parametrize("skill", ["taskmanager", "dispatcher"])
def test_skill_says_how_to_install_tm_before_its_first_guide_line(skill: str) -> None:
    text = (_plugin_root() / "skills" / skill / "SKILL.md").read_text(encoding="utf-8")
    body = text.split("\n---\n", 1)[1]
    assert INSTALL in body
    assert body.index(INSTALL) < body.index("tm guide")
    assert "When `tm` is not on PATH, say so and stop; run nothing in its place." in body
