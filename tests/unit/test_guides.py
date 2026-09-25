"""Every command a built-in guide prints is a command the CLI has.

An agent runs what the guide shows verbatim, so a renamed command or flag has to fail here rather
than in the agent's terminal.
"""

import re
import shlex
from importlib.resources import files
from pathlib import Path
from typing import Any

import pytest
import typer.main

from taskmanager.cli.main import _guide_topics, app

REQUIRED_TOPICS = {"implement", "review", "fix", "merge", "overview"}
COMMAND_FLOOR = 40

_SPAN = re.compile(r"`([^`\n]+)`")
_STOP = re.compile(r"[|;#]|&&")


def _topics() -> list[str]:
    return sorted(_guide_topics())


def _guide_text(topic: str) -> str:
    return str(files("taskmanager").joinpath(f"guides/{topic}.md").read_text(encoding="utf-8"))


def _snippets(text: str) -> list[str]:
    """Every fenced line and every inline code span, fences and backticks dropped."""
    found: list[str] = []
    fenced = False
    for line in text.splitlines():
        if line.startswith("```"):
            fenced = not fenced
            continue
        if fenced:
            found.append(line.strip())
        else:
            found.extend(m.group(1) for m in _SPAN.finditer(line))
    return [s for s in found if s]


def _tm_commands(text: str) -> list[str]:
    commands: list[str] = []
    for snippet in _snippets(text):
        head = _STOP.split(snippet)[0].strip()
        if head.startswith("tm ") and head not in commands:
            commands.append(head)
    return commands


def _tokens(command: str) -> list[str]:
    try:
        return shlex.split(command)[1:]
    except ValueError:
        return command.split()[1:]


def _resolve(tokens: list[str]) -> tuple[str, Any, list[str]]:
    """Walk the typer app down to the command the tokens name, returning it and the rest.

    Typer builds its own click-compatible classes rather than click's own, so a group is what
    carries subcommands, not what passes an isinstance check.
    """
    cmd: Any = typer.main.get_command(app)
    named = ["tm"]
    rest = list(tokens)
    while getattr(cmd, "commands", None) and rest and not rest[0].startswith("-"):
        sub = cmd.commands.get(rest[0])
        assert sub is not None, f"`{' '.join(named)}` has no subcommand {rest[0]!r}"
        named.append(rest.pop(0))
        cmd = sub
    return " ".join(named), cmd, rest


def _accepted_flags(cmd: Any) -> set[str]:
    flags = {"--help"}
    for param in cmd.params:
        flags.update(param.opts)
        flags.update(param.secondary_opts)
    return flags


_CASES = [(topic, cmd) for topic in _topics() for cmd in _tm_commands(_guide_text(topic))]


def test_every_topic_has_a_guide_file() -> None:
    topics = _topics()
    assert REQUIRED_TOPICS <= set(topics), f"missing role guides: {REQUIRED_TOPICS - set(topics)}"
    for topic in topics:
        assert _guide_text(topic).strip(), f"guide '{topic}' is empty"


@pytest.mark.parametrize("topic", _topics())
def test_guide_opens_with_a_title_and_a_one_sentence_blurb(topic: str) -> None:
    """`tm guide` lists a topic by the paragraph under its title, so that paragraph is one sentence."""
    text = _guide_text(topic)
    first, *_ = text.splitlines()
    assert first.startswith("# "), f"guide '{topic}' does not open with a `# ` title"

    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    assert len(paragraphs) >= 2, f"guide '{topic}' has no paragraph under its title"
    blurb = paragraphs[1]
    assert "\n" not in blurb, f"guide '{topic}' blurb is more than one line: {blurb!r}"
    assert blurb.endswith("."), f"guide '{topic}' blurb is not a sentence: {blurb!r}"
    assert ". " not in blurb, f"guide '{topic}' blurb is more than one sentence: {blurb!r}"


def test_the_guides_show_enough_commands_to_be_worth_checking() -> None:
    """A collection that silently shrinks to nothing would leave every case below green."""
    assert len(_CASES) >= COMMAND_FLOOR, f"only {len(_CASES)} `tm` commands found in the guides"
    for topic in _topics():
        assert _tm_commands(_guide_text(topic)), f"guide '{topic}' shows no `tm` command"


def _assert_command_exists(where: str, command: str) -> None:
    tokens = _tokens(command)
    if not tokens:
        return
    name, cmd, rest = _resolve(tokens)
    accepted = _accepted_flags(cmd)
    for token in rest:
        if not token.startswith("-"):
            continue
        flag = token.split("=", 1)[0]
        assert flag in accepted, f"`{name}` has no flag {flag} ({where} shows `{command}`)"


@pytest.mark.parametrize("topic,command", _CASES, ids=[f"{t}:{c}" for t, c in _CASES])
def test_guide_command_exists(topic: str, command: str) -> None:
    _assert_command_exists(f"{topic}.md", command)


RETIRED = (
    "tm run start",
    "tm run stop",
    "tm run heartbeat",
    "NOT_STARTED",
    "WAITING_FIXES",
    "IN_FLIGHT",
    ":hold",
    "external_blockers",
    "--release",
    "maxFixRounds",
)

CLOSING_VERBS = {
    "implement": "tm task complete <task-id> --agent <name>",
    "fix": "tm task complete <node-id> --agent <name>",
    "review": "tm task review <node-id> --agent <name> --approve",
    "merge": "tm job resume <job>",
}

WORKFLOW = Path(__file__).resolve().parents[2] / "workflows" / "tm-wave.js"


@pytest.mark.parametrize("topic", _topics())
def test_guide_carries_no_retired_lifecycle_vocabulary(topic: str) -> None:
    text = _guide_text(topic)
    assert [word for word in RETIRED if word in text] == []


@pytest.mark.parametrize("topic,verb", sorted(CLOSING_VERBS.items()))
def test_role_guide_shows_the_verb_that_closes_its_step(topic: str, verb: str) -> None:
    assert verb in _guide_text(topic)


def test_overview_carries_the_cycle_and_the_cutover_runbook() -> None:
    text = _guide_text("overview")
    for needle in (
        "READY ──claim──▶ IMPLEMENTING",
        "MERGING ──landed and verified──▶ COMPLETED",
        "## Moving an estate to this version",
        "`tm init --archive`",
        "`tm export <export dir>`",
    ):
        assert needle in text, needle


def test_dispatch_guide_names_every_argument_tm_wave_reads() -> None:
    read = set(re.findall(r"\bA\.([A-Za-z]+)", WORKFLOW.read_text(encoding="utf-8")))
    assert read, "no argument found in the workflow script"
    text = _guide_text("dispatch")
    assert sorted(arg for arg in read if f"`{arg}`" not in text) == []


REPO = Path(__file__).resolve().parents[2]
DOCS = (
    "README.md",
    "agents/tm-op.md",
    "commands/board.md",
    "commands/task.md",
    "commands/tm.md",
    "skills/dispatcher/SKILL.md",
    "skills/taskmanager/SKILL.md",
    "src/taskmanager/skills/dispatcher/SKILL.md",
    "src/taskmanager/skills/taskmanager/SKILL.md",
)


def _doc_text(doc: str) -> str:
    return (REPO / doc).read_text(encoding="utf-8")


_DOC_CASES = [
    (doc, cmd)
    for doc in DOCS
    for cmd in _tm_commands(_doc_text(doc))
    if not _tokens(cmd)[:1] or not _tokens(cmd)[0].startswith("$")
]


def test_the_docs_show_enough_commands_to_be_worth_checking() -> None:
    assert len(_DOC_CASES) >= 15, f"only {len(_DOC_CASES)} `tm` commands found in the shipped docs"


@pytest.mark.parametrize("doc,command", _DOC_CASES, ids=[f"{d}:{c}" for d, c in _DOC_CASES])
def test_doc_command_exists(doc: str, command: str) -> None:
    _assert_command_exists(doc, command)


@pytest.mark.parametrize("doc", DOCS)
def test_doc_carries_no_retired_lifecycle_vocabulary(doc: str) -> None:
    text = _doc_text(doc)
    assert [word for word in RETIRED if word in text] == []
