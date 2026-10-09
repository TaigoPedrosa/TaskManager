import re
from pathlib import Path

import pytest

PLUGIN = Path(__file__).resolve().parents[2] / "plugin"
COMMAND = PLUGIN / "commands" / "design.md"
SKILL = PLUGIN / "skills" / "design" / "SKILL.md"
SECTIONS = PLUGIN / "skills" / "design" / "references" / "design-sections.md"

STEPS = (
    "Start",
    "Scale",
    "Explore",
    "Ask",
    "Approaches",
    "Write the design",
    "Self-review",
    "Gate",
)
CITED = ("tm decision add", "tm decision list", "tm section set", "tm guide plan", "tm doctor")

# The skill designs work in any stack, from any tracker: naming one product reads as the only one.
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
    "pip",
    "npm",
    "pnpm",
    "yarn",
    "uv",
    "poetry",
    "maven",
    "gradle",
    "cargo",
    "bundler",
    "VS Code",
    "VSCode",
    "Vim",
    "Neovim",
    "Emacs",
    "IntelliJ",
    "PyCharm",
)


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _frontmatter(text: str) -> dict[str, str]:
    match = re.match(r"\A---\n(.*?)\n---\n", text, re.DOTALL)
    assert match, "no frontmatter"
    pairs = (line.split(":", 1) for line in match.group(1).splitlines())
    return {key.strip(): value.strip() for key, value in pairs}


def _steps() -> list[str]:
    return re.findall(r"^## \d+\. (.+)$", _text(SKILL), re.MULTILINE)


def test_the_design_skill_names_every_step_and_its_gate() -> None:
    assert _steps() == list(STEPS)
    text = _text(SKILL)
    assert "no plan is written" in text.lower()
    assert '"approve the design" decision is answered approve' in text
    assert "--blocks <spec-id>" in text.split("## 8. Gate", 1)[1]


def test_design_command_hands_the_call_to_the_design_skill() -> None:
    text = _text(COMMAND)
    assert _frontmatter(text).get("description")
    assert "`design` skill" in text
    assert "$ARGUMENTS" in text


def test_design_skill_is_named_design_and_triggers_on_a_spec_needing_a_design() -> None:
    head = _frontmatter(_text(SKILL))
    assert head.get("name") == "design"
    description = head.get("description", "").lower()
    for word in ("spec", "design", "decision", "plan"):
        assert word in description, description


@pytest.mark.parametrize("needle", CITED)
def test_design_skill_cites(needle: str) -> None:
    assert needle in _text(SKILL)


def test_design_skill_refuses_to_start_while_a_blocking_decision_is_open() -> None:
    start = _text(SKILL).split("## 1. Start", 1)[1].split("## 2.", 1)[0]
    assert "awaiting_decisions" in start
    assert "stop" in start


def test_design_skill_asks_one_question_at_a_time_through_decisions() -> None:
    ask = _text(SKILL).split("## 4. Ask", 1)[1].split("## 5.", 1)[0]
    assert "one question at a time" in ask
    assert "--recommend" in ask
    assert "--blocks <spec-id>" in ask
    assert "never from the conversation" in _text(SKILL)


def test_design_skill_takes_architectural_when_the_scale_is_in_doubt() -> None:
    scale = _text(SKILL).split("## 2. Scale", 1)[1].split("## 3.", 1)[0]
    assert "When in doubt, take architectural" in scale
    assert "overrule" in scale


def test_design_skill_writes_the_design_into_the_spec_from_the_reference() -> None:
    text = _text(SKILL)
    assert "tm section set <spec-id>:design" in text
    assert "references/design-sections.md" in text


@pytest.mark.parametrize(
    "heading",
    [
        "Approach",
        "Components and contracts",
        "Data flow",
        "Failure handling",
        "Out of scope",
        "Verification",
    ],
)
def test_design_sections_reference_holds_each_heading(heading: str) -> None:
    assert re.search(rf"^### {re.escape(heading)}$", _text(SECTIONS), re.MULTILINE)


def test_design_self_review_checks_placeholders_contradictions_ambiguity_and_scope() -> None:
    review = _text(SKILL).split("## 7. Self-review", 1)[1].split("## 8.", 1)[0]
    for word in ("Placeholders", "Contradictions", "Ambiguity", "Scope"):
        assert f"**{word}**" in review, word


@pytest.mark.parametrize("path", [COMMAND, SKILL, SECTIONS], ids=lambda p: p.name)
def test_design_text_names_no_tracker_language_test_runner_package_manager_or_editor(
    path: Path,
) -> None:
    text = _text(path)
    named = [w for w in DENIED if re.search(rf"\b{re.escape(w)}\b", text, re.IGNORECASE)]
    assert named == []
