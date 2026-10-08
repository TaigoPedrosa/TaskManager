"""A slash command runs a tm command; typing one grants nothing on the user's behalf."""

import re
from pathlib import Path

import pytest

COMMANDS = sorted((Path(__file__).resolve().parents[2] / "plugin" / "commands").glob("*.md"))

_FRONTMATTER = re.compile(r"\A---\n(?P<head>.*?)\n---\n(?P<body>.*)\Z", re.DOTALL)
_CODE_SPAN = re.compile(r"`([^`\n]+)`")
_GRANT = re.compile(
    r"opt[- ]?in|consent|authori[sz]|permission|on the user's behalf|mandatory", re.IGNORECASE
)
_UNEXPOSED_TOOLS = ("ScheduleWakeup",)
_RUNS = {"board": "tm web", "task": "tm task start", "tm": "tm $ARGUMENTS"}


def _parse(path: Path) -> tuple[str, str]:
    match = _FRONTMATTER.match(path.read_text(encoding="utf-8"))
    assert match, f"{path.name} has no frontmatter"
    return match["head"], match["body"]


def test_the_plugin_ships_every_command_checked_here() -> None:
    assert sorted(path.stem for path in COMMANDS) == sorted(_RUNS)


@pytest.mark.parametrize("path", COMMANDS, ids=lambda path: path.name)
def test_command_declares_no_grant_on_the_users_behalf(path: Path) -> None:
    _, body = _parse(path)
    assert [match.group(0) for match in _GRANT.finditer(body)] == []


@pytest.mark.parametrize("path", COMMANDS, ids=lambda path: path.name)
def test_command_names_no_tool_the_harness_does_not_expose(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    assert [tool for tool in _UNEXPOSED_TOOLS if tool in text] == []


@pytest.mark.parametrize("path", COMMANDS, ids=lambda path: path.name)
def test_command_runs_its_tm_command(path: Path) -> None:
    head, body = _parse(path)
    assert re.search(r"^description: \S", head, re.MULTILINE)
    assert any(span.startswith(_RUNS[path.stem]) for span in _CODE_SPAN.findall(body))
