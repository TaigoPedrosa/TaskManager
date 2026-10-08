"""`tm guide <role>` carries a codegraph block exactly where codegraph can answer it.

An agent runs a block's commands verbatim, so each one is checked against codegraph 1.6's own help.
"""

import re
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import Self

import pytest
from typer.testing import CliRunner

from taskmanager.cli.main import app

ROLES = ["fix", "implement", "plan", "review"]
NEEDED_COMMANDS = {"explore", "node", "callers", "impact", "affected", "sync"}
SEPARATOR = "\n\n---\n\n"
OPENING = "The repository has a codegraph index"
# Resolved before any test narrows PATH to its fake tools.
GIT = shutil.which("git") or "git"

# `codegraph --help`, then `codegraph <command> --help` for each command a block runs, of
# codegraph 1.6.0, verbatim.
TOP_HELP_1_6 = """\
Usage: codegraph [options] [command]

Code intelligence and knowledge graph for any codebase

Options:
  -V, --version                  output the version number
  --color                        force ANSI colors even when stdout is not a TTY
  --no-color                     disable ANSI colors (NO_COLOR env is also
                                 honored)
  -h, --help                     display help for command

Commands:
  init [options] [path]          Initialize CodeGraph in a project directory and
                                 build the initial index
  uninit [options] [path]        Remove CodeGraph from a project (deletes
                                 .codegraph/ directory)
  index [options] [path]         Rebuild the full index from scratch (same
                                 result as a fresh init)
  sync [options] [path]          Sync changes since last index
  status [options] [path]        Show index status and statistics
  query [options] <search>       Search for symbols in the codebase
  explore [options] <query...>   Explore an area: relevant symbols' source +
                                 call paths in one shot (same output as the
                                 codegraph_explore MCP tool)
  context [options] <task...>    Build context for a task: relevant symbols,
                                 relationships, and code blocks
  node [options] [name]          One symbol's source + caller/callee trail, or
                                 read a file with line numbers + dependents
                                 (same output as the codegraph_node MCP tool)
  files [options]                Show project file structure from the index
  daemon|daemons                 Manage running CodeGraph background daemons —
                                 pick one and press enter to stop it
  unlock [path]                  Remove a stale lock file that is blocking
                                 indexing
  callers [options] <symbol>     Find all functions/methods that call a specific
                                 symbol
  callees [options] <symbol>     Find all functions/methods that a specific
                                 symbol calls
  impact [options] <symbol>      Analyze what code is affected by changing a
                                 symbol
  affected [options] [files...]  Find test files affected by changed source
                                 files
  install [options]              Install codegraph MCP server into one or more
                                 agents (Claude Code, Cursor, Codex CLI,
                                 opencode, Hermes Agent, Gemini CLI, Antigravity
                                 IDE, Kiro, GitHub Copilot)
  uninstall [options]            Remove codegraph from your agents (Claude Code,
                                 Cursor, Codex CLI, opencode, Hermes Agent,
                                 Gemini CLI, Antigravity IDE, Kiro, GitHub
                                 Copilot)
  telemetry [action]             Show or change anonymous usage telemetry
                                 (status, on, off)
  upgrade [options] [version]    Update CodeGraph to the latest release (or a
                                 specific version)
  version                        Print the installed CodeGraph version (also:
                                 -v, --version)
  help [command]                 display help for command
"""

COMMAND_HELP_1_6 = {
    "explore": """\
Usage: codegraph explore [options] <query...>

Explore an area: relevant symbols' source + call paths in one shot (same output
as the codegraph_explore MCP tool)

Options:
  -p, --path <path>     Project path
  --max-files <number>  Maximum number of files to include source from
  -h, --help            display help for command
""",
    "node": """\
Usage: codegraph node [options] [name]

One symbol's source + caller/callee trail, or read a file with line numbers +
dependents (same output as the codegraph_node MCP tool)

Options:
  -p, --path <path>  Project path
  -f, --file <file>  Treat as file mode (or disambiguate a symbol to this file)
  --offset <number>  File mode: 1-based start line
  --limit <number>   File mode: maximum lines
  --symbols-only     File mode: just the symbol map + dependents
  -h, --help         display help for command
""",
    "callers": """\
Usage: codegraph callers [options] <symbol>

Find all functions/methods that call a specific symbol

Options:
  -p, --path <path>     Project path
  -l, --limit <number>  Maximum results (default: "20")
  -j, --json            Output as JSON
  -h, --help            display help for command
""",
    "impact": """\
Usage: codegraph impact [options] <symbol>

Analyze what code is affected by changing a symbol

Options:
  -p, --path <path>     Project path
  -d, --depth <number>  Traversal depth (default: "2")
  -j, --json            Output as JSON
  -h, --help            display help for command
""",
    "affected": """\
Usage: codegraph affected [options] [files...]

Find test files affected by changed source files

Options:
  -p, --path <path>     Project path
  --stdin               Read file list from stdin (one per line)
  -d, --depth <number>  Max dependency traversal depth (default: "5")
  -f, --filter <glob>   Custom glob filter for test files (e.g. "e2e/*.spec.ts")
  -j, --json            Output as JSON
  -q, --quiet           Only output file paths, no decoration
  -h, --help            display help for command
""",
    "sync": """\
Usage: codegraph sync [options] [path]

Sync changes since last index

Options:
  -q, --quiet  Suppress output (for git hooks)
  -h, --help   display help for command
""",
}

_COMMAND = re.compile(r"^  (\w+)(?:\|\w+)?\s", re.MULTILINE)
_OPTION = re.compile(r"^  (?:(-\w), )?(--[\w-]+)( <[\w-]+>)?", re.MULTILINE)
_ARG = re.compile(r"<[^>]+>|\[[^\]]+\]")
_SPAN = re.compile(r"`([^`\n]+)`")
_STOP = re.compile(r"[|;]|&&")


@dataclass(frozen=True)
class Usage:
    takes_value: dict[str, bool]
    required: int
    most: int | None

    @classmethod
    def of(cls, help_text: str) -> Self:
        _, after_options = help_text.splitlines()[0].split("[options]", 1)
        args = _ARG.findall(after_options)
        takes_value = {
            flag: bool(value)
            for short, long, value in _OPTION.findall(help_text)
            for flag in (short, long)
            if flag
        }
        variadic = any(arg[-4:-1] == "..." for arg in args)
        return cls(
            takes_value, sum(arg.startswith("<") for arg in args), None if variadic else len(args)
        )


COMMANDS_1_6 = set(_COMMAND.findall(TOP_HELP_1_6.split("Commands:", 1)[1]))
USAGES_1_6 = {name: Usage.of(text) for name, text in COMMAND_HELP_1_6.items()}


def _assert_runs_on_codegraph_1_6(command: str) -> None:
    name, *tokens = shlex.split(command)[1:]
    assert name in COMMANDS_1_6, f"codegraph 1.6 has no command `{name}` (`{command}`)"
    assert name in USAGES_1_6, f"no codegraph 1.6 help recorded for `{name}` (`{command}`)"
    usage = USAGES_1_6[name]
    positional = 0
    rest = iter(tokens)
    for token in rest:
        if not token.startswith("-"):
            positional += 1
            continue
        assert token in usage.takes_value, f"`codegraph {name}` has no flag {token} (`{command}`)"
        if usage.takes_value[token]:
            value = next(rest, None)
            assert value is not None and not value.startswith("-"), (
                f"`codegraph {name} {token}` takes a value (`{command}`)"
            )
    assert positional >= usage.required, f"`codegraph {name}` misses an argument (`{command}`)"
    assert usage.most is None or positional <= usage.most, (
        f"`codegraph {name}` takes at most {usage.most} arguments (`{command}`)"
    )


def _codegraph_commands(text: str) -> list[str]:
    return [
        segment.strip()
        for span in _SPAN.findall(text)
        for segment in _STOP.split(span)
        if segment.strip().startswith("codegraph ")
    ]


def _tool(bin_dir: Path, name: str, script: str) -> None:
    path = bin_dir / name
    path.write_text(f"#!/bin/sh\n{script}\n", encoding="utf-8")
    path.chmod(0o755)


@pytest.fixture
def bin_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The only directory on PATH, so codegraph is present exactly when a test writes it."""
    path = tmp_path / "bin"
    path.mkdir()
    monkeypatch.setenv("PATH", str(path))
    _tool(path, "git", f'exec "{GIT}" "$@"')
    return path


@pytest.fixture
def project(tmp_path: Path, bin_dir: Path) -> Path:
    path = tmp_path / "project"
    res = subprocess.run([GIT, "init", "-q", str(path)], capture_output=True, check=False)
    assert res.returncode == 0, res.stderr
    return path


def _install_codegraph(bin_dir: Path) -> None:
    _tool(bin_dir, "codegraph", "echo 1.6.0")


def _index(project: Path) -> None:
    (project / ".codegraph").mkdir()


def _guide(project: Path, *args: str) -> str:
    result = CliRunner().invoke(app, ["guide", *args, "-C", str(project)])
    assert result.exit_code == 0, result.output
    return result.stdout


def _builtin(topic: str) -> str:
    return files("taskmanager").joinpath(f"guides/{topic}.md").read_text(encoding="utf-8").rstrip()


def _block(project: Path, topic: str) -> str:
    out = _guide(project, topic)
    head = _builtin(topic) + SEPARATOR
    assert out.startswith(head), f"`tm guide {topic}` does not open with its built-in text"
    return out[len(head) :].rstrip("\n")


@pytest.fixture
def ready(project: Path, bin_dir: Path) -> Path:
    _install_codegraph(bin_dir)
    _index(project)
    return project


@pytest.mark.parametrize("topic", ROLES)
def test_guide_with_codegraph_and_an_index_appends_the_role_s_block(
    topic: str, ready: Path
) -> None:
    block = _block(ready, topic)

    assert block.startswith(OPENING)
    assert not re.search(r"^#", block, re.MULTILINE), "a block carries a heading of the file"
    assert f"## {topic}\n\n{block}\n" in _builtin("codegraph") + "\n", "another role's block"
    assert _codegraph_commands(block)


@pytest.mark.parametrize("topic", ROLES)
def test_guide_without_codegraph_prints_the_builtin_guide_alone(topic: str, project: Path) -> None:
    _index(project)

    assert _guide(project, topic) == _builtin(topic) + "\n"


@pytest.mark.parametrize("topic", ROLES)
def test_guide_with_codegraph_and_no_index_prints_the_builtin_guide_alone(
    topic: str, project: Path, bin_dir: Path
) -> None:
    _install_codegraph(bin_dir)

    assert _guide(project, topic) == _builtin(topic) + "\n"


def test_guide_for_a_role_with_no_block_prints_the_builtin_guide_alone(ready: Path) -> None:
    assert _guide(ready, "merge") == _builtin("merge") + "\n"


def test_guide_puts_the_block_between_the_builtin_text_and_the_project_addendum(
    ready: Path,
) -> None:
    block = _block(ready, "implement")
    runner = CliRunner()
    for args in (
        ["init"],
        ["spec", "add", "Project guide", "--slug", "guide"],
        ["section", "set", "guide:implement", "Local rule: ask first."],
    ):
        result = runner.invoke(app, [*args, "-C", str(ready)])
        assert result.exit_code == 0, result.output

    assert (
        _guide(ready, "implement")
        == SEPARATOR.join([_builtin("implement"), block, "Local rule: ask first."]) + "\n"
    )
    assert (
        _guide(ready, "implement", "--builtin")
        == SEPARATOR.join([_builtin("implement"), block]) + "\n"
    )
    assert _guide(ready, "implement", "--project") == "Local rule: ask first.\n"


def test_guide_lists_no_codegraph_topic(ready: Path) -> None:
    assert "codegraph" not in _guide(ready)
    assert CliRunner().invoke(app, ["guide", "codegraph", "-C", str(ready)]).exit_code != 0


@pytest.mark.parametrize("topic", ROLES)
def test_codegraph_block_commands_run_on_codegraph_1_6(topic: str, ready: Path) -> None:
    for command in _codegraph_commands(_block(ready, topic)):
        _assert_runs_on_codegraph_1_6(command)


def test_codegraph_blocks_name_every_command_an_agent_needs(ready: Path) -> None:
    named = {
        shlex.split(command)[1]
        for topic in ROLES
        for command in _codegraph_commands(_block(ready, topic))
    }

    assert NEEDED_COMMANDS <= named, f"no block names {sorted(NEEDED_COMMANDS - named)}"


@pytest.mark.parametrize(
    "command",
    [
        "codegraph impacts <symbol>",
        "codegraph impact --limit 5 <symbol>",
        "codegraph explore -p",
        "codegraph callers -p <repo>",
        "codegraph sync <one> <two>",
    ],
)
def test_codegraph_1_6_check_refuses_a_command_codegraph_1_6_would_refuse(command: str) -> None:
    with pytest.raises(AssertionError):
        _assert_runs_on_codegraph_1_6(command)


def test_codegraph_1_6_check_takes_a_variadic_argument_and_a_flag_value() -> None:
    _assert_runs_on_codegraph_1_6("codegraph affected -p <repo> -f '<glob>' -q <a> <b> <c>")
    _assert_runs_on_codegraph_1_6('codegraph explore --max-files 5 "<the area>"')
