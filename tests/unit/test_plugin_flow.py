from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PLUGIN = ROOT / "plugin"
GUIDES = ROOT / "src" / "taskmanager" / "guides"

FLOW = ("`tm init`", "`/taskmanager:init", "`/taskmanager:design", "`/taskmanager:plan")
FOREIGN = ("superpowers", "brainstorming", "writing-plans")


def _steps_in_order(text: str) -> list[int]:
    return [text.index(step) for step in FLOW]


def test_init_hands_off_to_design_and_no_guide_names_another_plugin() -> None:
    skill = (PLUGIN / "skills" / "init" / "SKILL.md").read_text()
    hand_off = skill.split("## 6. Hand off", 1)[1].split("\n## ", 1)[0]
    assert "- the next stage: `/taskmanager:design <spec-id>`" in hand_off
    assert "Brainstorm" not in skill

    command = (PLUGIN / "commands" / "init.md").read_text()
    assert "`/taskmanager:design <spec-id>` as the next stage" in command

    offenders = [
        f"{path.relative_to(ROOT)}: {word}"
        for base in (PLUGIN, GUIDES)
        for path in sorted(base.rglob("*"))
        if path.is_file()
        for word in FOREIGN
        if word in path.read_text().lower()
    ]
    assert not offenders, offenders


@pytest.mark.parametrize(
    ("path", "heading"),
    [
        (PLUGIN / "skills" / "taskmanager" / "SKILL.md", "## The flow"),
        (GUIDES / "overview.md", "## From a source to dispatch"),
        (ROOT / "README.md", "## Quickstart"),
    ],
    ids=["taskmanager-skill", "overview-guide", "readme"],
)
def test_flow_names_init_design_plan_then_dispatch_in_order(path: Path, heading: str) -> None:
    section = path.read_text().split(heading, 1)[1].split("\n## ", 1)[0]
    positions = _steps_in_order(section)
    assert positions == sorted(positions)
    assert "dispatch" in section[positions[-1] :]
