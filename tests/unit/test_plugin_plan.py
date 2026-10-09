import re
from pathlib import Path

import pytest
from test_plugin_init import DENIED as INIT_DENIED

PLUGIN = Path(__file__).resolve().parents[2] / "plugin"
COMMAND = PLUGIN / "commands" / "plan.md"
SKILL = PLUGIN / "skills" / "plan" / "SKILL.md"
CHECKLIST = PLUGIN / "skills" / "plan" / "references" / "plan-checklist.md"

STEPS = (
    "Gate",
    "Read",
    "Slice",
    "Check the document",
    "Import",
    "Prove",
    "Self-review",
    "Hand off",
)
CITED = (
    "tm guide plan",
    "tm import",
    "tm verify run",
    "tm render",
    "tm decision list",
    "tm wave discover",
    "references/plan-checklist.md",
)
CHECKLIST_ITEMS = {
    "target_repo": r"`target_repo` on every task",
    "declared_files": r"`declared_files` complete and disjoint",
    "tests count": r"tests included",
    "fails before the change": r"verification that fails before the change",
    "proved after import": r"tm verify run <task-id> --ref",
    "joined verification": r"joined verification on a reviewed plan",
    "overview": r"plan's `overview`",
    "write paths": r"invariant names every write path",
    "reproduction": r"bug fix replays its reproduction",
    "config key": r"Limits name a config key",
    "design frame": r"design frame `requires`",
    "migration": r"migration is marked sensitive",
}

# Package managers on top of the init skill's trackers, languages, test runners and editors.
DENIED = (*INIT_DENIED, "npm", "yarn", "pnpm", "pip", "poetry", "uv", "maven", "gradle")


def _frontmatter(text: str) -> dict[str, str]:
    match = re.match(r"\A---\n(.*?)\n---\n", text, re.DOTALL)
    assert match, "no frontmatter"
    pairs = (line.split(":", 1) for line in match.group(1).splitlines())
    return {key.strip(): value.strip() for key, value in pairs}


def _section(text: str, heading: str) -> str:
    match = re.search(
        rf"^## \d+\. {re.escape(heading)}\n(.*?)(?=^## |\Z)", text, re.MULTILINE | re.DOTALL
    )
    assert match, heading
    return match.group(1)


def test_the_plan_skill_gates_on_design_approval_and_proves_its_verifications() -> None:
    text = SKILL.read_text(encoding="utf-8")
    gate = _section(text, "Gate")
    assert "approve the design" in gate
    assert "Approve the design of <spec-id>?" in gate
    assert "`ANSWERED` and its `answer.option` is `approve`" in gate
    assert "stop and name it" in gate
    prove = _section(text, "Prove")
    assert "tm verify run <task-id> --ref origin/<lands_on>" in prove
    assert "must exit 1" in prove
    assert "rewrite it" in prove
    assert "no plan before the design is approved" in text
    assert "no dispatch before the plan is imported" in text


def test_plan_command_hands_the_call_to_the_plan_skill() -> None:
    text = COMMAND.read_text(encoding="utf-8")
    assert _frontmatter(text).get("description")
    assert "`plan` skill" in text
    assert "$ARGUMENTS" in text


def test_plan_skill_is_named_plan_and_triggers_on_an_approved_design() -> None:
    head = _frontmatter(SKILL.read_text(encoding="utf-8"))
    assert head.get("name") == "plan"
    description = head.get("description", "").lower()
    for word in ("design", "approved", "plans", "tasks"):
        assert word in description, description


def test_plan_skill_names_every_step_in_order() -> None:
    headings = re.findall(r"^## \d+\. (.+)$", SKILL.read_text(encoding="utf-8"), re.MULTILINE)
    assert headings == list(STEPS)


@pytest.mark.parametrize("needle", CITED)
def test_plan_skill_cites(needle: str) -> None:
    assert needle in SKILL.read_text(encoding="utf-8")


@pytest.mark.parametrize("item", CHECKLIST_ITEMS, ids=str)
def test_plan_checklist_names_the_item_and_its_guide_section(item: str) -> None:
    line = next(
        (
            line
            for line in CHECKLIST.read_text(encoding="utf-8").splitlines()
            if re.search(CHECKLIST_ITEMS[item], line)
        ),
        "",
    )
    assert re.search(r"\(`tm guide plan` §\d+", line), line


def test_plan_skill_writes_its_document_to_scratch_never_a_markdown_file_in_the_repo() -> None:
    text = SKILL.read_text(encoding="utf-8")
    blocks = "\n".join(re.findall(r"^```\n(.*?)^```$", text, re.MULTILINE | re.DOTALL))
    assert 'doc="$(mktemp -d)/plan.yaml"' in blocks
    assert 'tm import --format yaml --file "$doc"' in blocks
    assert re.findall(r"\S+\.md\b", blocks) == []
    assert "tm export" not in text
    assert "Never write the plan as a markdown file in the repo" in text


def test_plan_skill_raises_no_question_in_chat() -> None:
    text = SKILL.read_text(encoding="utf-8").lower()
    assert "ask the user" not in text
    assert "ask the owner" not in text


@pytest.mark.parametrize("path", [COMMAND, SKILL, CHECKLIST], ids=lambda p: p.name)
def test_plan_text_names_no_tracker_language_runner_package_manager_or_editor(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    named = [w for w in DENIED if re.search(rf"\b{re.escape(w)}\b", text, re.IGNORECASE)]
    assert named == []
