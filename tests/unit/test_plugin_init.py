import re
from pathlib import Path

import pytest

PLUGIN = Path(__file__).resolve().parents[2] / "plugin"
COMMAND = PLUGIN / "commands" / "init.md"
SKILL = PLUGIN / "skills" / "init" / "SKILL.md"
TEMPLATE = PLUGIN / "skills" / "init" / "references" / "spec-template.md"

STEPS = ("Ready check", "Source", "Record", "Repos", "Landing branch", "Hand off")
CITED = ("tm guide intake", "tm init", "tm doctor", "land_on", "tm decision add")

# The skill starts work from any tracker, in any stack: naming one product reads as the only one.
DENIED = (
    "Jira",
    "Linear",
    "Notion",
    "Confluence",
    "Asana",
    "Trello",
    "ClickUp",
    "YouTrack",
    "Azure DevOps",
    "GitHub",
    "GitLab",
    "Bitbucket",
    "Slack",
    "Python",
    "JavaScript",
    "TypeScript",
    "Java",
    "Kotlin",
    "Golang",
    "Rust",
    "Ruby",
    "pytest",
    "unittest",
    "jest",
    "vitest",
    "mocha",
    "JUnit",
    "RSpec",
    "go test",
    "cargo test",
    "VS Code",
    "VSCode",
    "Vim",
    "Neovim",
    "Emacs",
    "IntelliJ",
    "PyCharm",
)


def _frontmatter(text: str) -> dict[str, str]:
    match = re.match(r"\A---\n(.*?)\n---\n", text, re.DOTALL)
    assert match, "no frontmatter"
    pairs = (line.split(":", 1) for line in match.group(1).splitlines())
    return {key.strip(): value.strip() for key, value in pairs}


def test_init_command_hands_the_call_to_the_init_skill() -> None:
    text = COMMAND.read_text(encoding="utf-8")
    assert _frontmatter(text).get("description")
    assert "`init` skill" in text
    assert "$ARGUMENTS" in text


def test_init_skill_is_named_init_and_triggers_on_starting_work() -> None:
    head = _frontmatter(SKILL.read_text(encoding="utf-8"))
    assert head.get("name") == "init"
    description = head.get("description", "").lower()
    for word in ("start", "spec", "source"):
        assert word in description, description


@pytest.mark.parametrize("step", STEPS)
def test_init_skill_names_each_step_as_a_heading(step: str) -> None:
    headings = re.findall(r"^## \d+\. (.+)$", SKILL.read_text(encoding="utf-8"), re.MULTILINE)
    assert step in headings, headings


def test_init_skill_steps_run_in_order() -> None:
    headings = re.findall(r"^## \d+\. (.+)$", SKILL.read_text(encoding="utf-8"), re.MULTILINE)
    assert [h for h in headings if h in STEPS] == list(STEPS)


@pytest.mark.parametrize("needle", CITED)
def test_init_skill_cites(needle: str) -> None:
    assert needle in SKILL.read_text(encoding="utf-8")


@pytest.mark.parametrize("path", [COMMAND, SKILL, TEMPLATE], ids=lambda p: p.name)
def test_init_text_names_no_tracker_language_test_runner_or_editor(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    named = [w for w in DENIED if re.search(rf"\b{re.escape(w)}\b", text, re.IGNORECASE)]
    assert named == []


def test_init_skill_keeps_the_source_verbatim_and_marks_its_silences() -> None:
    text = SKILL.read_text(encoding="utf-8")
    assert "tm section set <spec-id>:source" in text
    assert "tm section set <spec-id>:context" in text
    assert "verbatim" in text
    assert "not stated in the source" in text


def test_init_skill_reruns_without_clobbering() -> None:
    text = SKILL.read_text(encoding="utf-8")
    assert "## Re-running" in text
    assert "Never move or delete an existing branch" in text


def test_init_skill_never_scrapes_an_auth_walled_source() -> None:
    assert "never scrape" in SKILL.read_text(encoding="utf-8").lower()


def test_spec_template_holds_the_context_skeleton() -> None:
    text = TEMPLATE.read_text(encoding="utf-8")
    for heading in ("What", "Why", "Scope", "Contracts", "Constraints", "Repos"):
        assert re.search(rf"^### {heading}\b", text, re.MULTILINE), heading
    assert "not stated in the source" in text
    assert "references/spec-template.md" in SKILL.read_text(encoding="utf-8")


def test_taskmanager_skill_points_to_init_for_starting_work() -> None:
    row = next(
        line
        for line in (PLUGIN / "skills" / "taskmanager" / "SKILL.md")
        .read_text(encoding="utf-8")
        .splitlines()
        if "/taskmanager:init" in line
    )
    assert "start" in row.lower()
