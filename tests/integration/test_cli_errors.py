"""What a user sees when a command fails or is asked for help: the message and its fix on
stderr, an exit code, never a traceback."""

import json
import subprocess
from pathlib import Path

import pytest
import typer.main
from click.testing import Result
from typer.testing import CliRunner

from taskmanager.cli.main import app

runner = CliRunner()


def tm(root: Path, *args: str, stdin: str | None = None) -> Result:
    return runner.invoke(app, [*args, "-C", str(root)], input=stdin)


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout


def no_traceback(res: Result) -> None:
    assert res.exception is None or isinstance(res.exception, SystemExit), res.exception
    assert "Traceback" not in res.output


@pytest.fixture
def root(tmp_path: Path) -> Path:
    assert tm(tmp_path, "init").exit_code == 0
    return tmp_path


PLAN_YAML = """\
spec: {id: S1, title: Spec}
plans:
  - id: S1-P1
    title: Plan
    tasks:
      - {id: S1-P1-a, title: Task a}
"""


def test_import_reads_a_yaml_file_by_its_suffix_and_names_what_it_created_and_updated(
    root: Path,
) -> None:
    plan = root / "plan.yaml"
    plan.write_text(PLAN_YAML, encoding="utf-8")

    first = tm(root, "import", "-f", str(plan))
    again = tm(root, "import", "-f", str(plan))

    assert first.exit_code == 0, first.output
    assert "created: S1, S1-P1, S1-P1-a\nupdated: -\n" in first.stdout
    assert again.exit_code == 0, again.output
    assert "created: -\nupdated: S1, S1-P1, S1-P1-a\n" in again.stdout


@pytest.mark.parametrize(
    ("name", "content", "reason"),
    [
        ("bad.yaml", "spec: [unclosed\n", "while parsing"),
        ("bad.json", "{not json", "Expecting property name"),
        ("bad.md", "no frontmatter here", "a markdown import needs a `---` frontmatter block"),
        ("list.yaml", "- a\n- b\n", "the document is not a mapping"),
        ("noid.yaml", "plans:\n  - title: Plan\n", "malformed document"),
    ],
)
def test_import_of_a_bad_document_is_refused_with_its_file_and_reason(
    root: Path, name: str, content: str, reason: str
) -> None:
    bad = root / name
    bad.write_text(content, encoding="utf-8")

    res = tm(root, "import", "-f", str(bad))

    assert res.exit_code == 1
    assert res.stdout == ""
    assert res.stderr.startswith(f"import refused, nothing written: {bad}: ")
    assert reason in res.stderr
    no_traceback(res)


def test_import_of_a_missing_file_is_refused_with_its_file(root: Path) -> None:
    missing = root / "nope.yaml"

    res = tm(root, "import", "-f", str(missing))

    assert res.exit_code == 1
    assert res.stderr.startswith(f"import refused, nothing written: {missing}: ")
    no_traceback(res)


def test_every_command_says_what_it_is_for() -> None:
    def undocumented(command: object, name: str) -> list[str]:
        subs = getattr(command, "commands", None)
        if subs is not None:
            return [m for k, sub in subs.items() for m in undocumented(sub, f"{name} {k}")]
        return [] if (getattr(command, "help", None) or "").strip() else [name]

    assert undocumented(typer.main.get_command(app), "tm") == []


def test_an_error_goes_to_stderr_and_leaves_stdout_empty(root: Path) -> None:
    res = tm(root, "task", "get", "NOPE", "--json")

    assert res.exit_code == 1
    assert res.stdout == ""
    assert res.stderr == "Task 'NOPE' not found\n"


def test_a_refusal_goes_to_stderr(root: Path) -> None:
    res = tm(root, "task", "start", "NOPE", "--agent", "a", "--session", "s")

    assert res.exit_code == 1
    assert res.stdout == ""
    assert res.stderr == "node 'NOPE' not found\n"


@pytest.mark.parametrize("argv", [["-h"], ["task", "-h"], ["web", "-h"], ["section", "set", "-h"]])
def test_dash_h_is_help_everywhere(argv: list[str]) -> None:
    res = runner.invoke(app, argv)

    assert res.exit_code == 0, res.output
    assert "Usage:" in res.stdout


def test_init_in_a_repository_excludes_the_estate_and_worktrees_and_names_the_next_step(
    tmp_path: Path,
) -> None:
    git(tmp_path, "init", "-q")
    (tmp_path / ".worktrees").mkdir()
    (tmp_path / ".worktrees" / "keep").write_text("", encoding="utf-8")

    first = tm(tmp_path, "init")
    second = tm(tmp_path, "init")

    assert first.exit_code == 0, first.output
    assert second.exit_code == 0, second.output
    assert "next: read `tm guide overview`" in first.stdout
    assert 'tm config set repos.<repo>.gates.main.command "<command>"' in first.stdout
    exclude = (tmp_path / ".git" / "info" / "exclude").read_text(encoding="utf-8")
    assert exclude.splitlines().count("/.taskmanager/") == 1
    assert exclude.splitlines().count("/.worktrees/") == 1
    assert git(tmp_path, "status", "--porcelain", "--untracked-files=all") == ""


def test_init_appends_to_an_exclude_file_that_ends_without_a_newline(tmp_path: Path) -> None:
    git(tmp_path, "init", "-q")
    exclude = tmp_path / ".git" / "info" / "exclude"
    exclude.write_text("*.log", encoding="utf-8")

    res = tm(tmp_path, "init")

    assert res.exit_code == 0, res.output
    assert exclude.read_text(encoding="utf-8") == "*.log\n/.taskmanager/\n/.worktrees/\n"


def test_init_leaves_a_worktree_dir_outside_the_repository_out_of_its_exclude(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    monkeypatch.setenv("TM_WORKTREES", str(tmp_path / "elsewhere"))

    res = tm(repo, "init")

    assert res.exit_code == 0, res.output
    exclude = (repo / ".git" / "info" / "exclude").read_text(encoding="utf-8")
    assert exclude.splitlines()[-1] == "/.taskmanager/"
    assert "elsewhere" not in exclude


def test_init_outside_a_repository_writes_no_exclude(tmp_path: Path) -> None:
    res = tm(tmp_path, "init")

    assert res.exit_code == 0, res.output
    assert not (tmp_path / ".git").exists()


def test_wave_discover_needs_no_slot_flags(root: Path) -> None:
    res = tm(root, "wave", "discover", "--session", "s")

    assert res.exit_code == 0, res.output
    assert res.stdout.splitlines()[-1].startswith("__CHECK n=0 ")


def test_semantic_search_without_a_provider_names_the_values_to_set(root: Path) -> None:
    res = tm(root, "search", "--mode", "semantic", "keys")

    assert res.exit_code == 1
    assert res.stderr == (
        "no embedding provider configured: `tm config set embeddings.provider <local|openai>`\n"
    )
    no_traceback(res)


def test_the_overview_says_how_to_turn_on_search_by_meaning(root: Path) -> None:
    res = tm(root, "guide", "overview")

    assert res.exit_code == 0, res.output
    search = res.stdout.split("\n## Search\n", 1)[1].split("\n## ", 1)[0]
    assert "`tm config set embeddings.provider local`" in search
    assert "`openai`" in search
    assert "`tm index`" in search


def test_audit_names_the_agent_that_closed_a_step_as_its_actor(tmp_path: Path) -> None:
    origin = tmp_path / "core.git"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    git(tmp_path, "clone", "-q", str(origin), str(tmp_path / "core"))
    work = tmp_path / "core"
    git(
        work,
        "-c",
        "user.email=ci@example.com",
        "-c",
        "user.name=CI",
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        "init",
    )
    git(work, "push", "-q", "origin", "HEAD:main")
    assert tm(tmp_path, "init").exit_code == 0
    assert tm(tmp_path, "config", "set", "repos.core.gates.main.command", "true").exit_code == 0
    plan = tmp_path / "plan.yaml"
    plan.write_text(
        PLAN_YAML.replace(
            "{id: S1-P1-a, title: Task a}",
            "{id: S1-P1-a, title: a, target_repo: core}\n"
            "      - {id: S1-P1-b, title: b, target_repo: core}",
        ),
        encoding="utf-8",
    )
    assert tm(tmp_path, "import", "-f", str(plan)).exit_code == 0

    def claim(node: str, agent: str) -> str:
        res = tm(tmp_path, "task", "start", node, "--agent", agent, "--session", "s", "--json")
        assert res.exit_code == 0, res.output
        token: str = json.loads(res.stdout)["token"]
        return token

    def run(*argv: str) -> None:
        res = tm(tmp_path, *argv)
        assert res.exit_code == 0, res.output

    token = claim("S1-P1-a", "builder")
    run("task", "complete", "S1-P1-a", "--agent", "builder", "--token", token)
    token = claim("S1-P1-a", "reviewer")
    run("section", "set", "S1-P1-a:review", "nothing open")
    run("task", "review", "S1-P1-a", "--approve", "--agent", "reviewer", "--token", token)
    token = claim("S1-P1-b", "leaver")
    run("task", "release", "S1-P1-b", "--agent", "leaver", "--token", token)

    events = json.loads(tm(tmp_path, "audit", "list", "--json").stdout)
    acted = {(e["target_id"], e["command"], e["actor_id"]) for e in events}

    assert {
        ("S1-P1-a", "task start", "builder"),
        ("S1-P1-a", "task complete", "builder"),
        ("S1-P1-a", "task start", "reviewer"),
        ("S1-P1-a", "task review", "reviewer"),
        ("S1-P1-b", "task start", "leaver"),
        ("S1-P1-b", "task release", "leaver"),
    } <= acted


def test_web_run_is_gone_and_web_alone_serves(root: Path) -> None:
    res = tm(root, "web", "run", "--no-open")

    assert res.exit_code == 2
    assert "No such command 'run'" in res.output


def test_search_highlights_read_as_bold_not_as_a_link(root: Path) -> None:
    assert tm(root, "spec", "add", "Signing keys", "--slug", "S1").exit_code == 0
    assert tm(root, "section", "set", "S1:notes", "rotate (a, b) now").exit_code == 0

    res = tm(root, "search", "--mode", "fts", "rotate")

    assert res.exit_code == 0, res.output
    assert "**rotate** (a, b)" in res.stdout
    assert "[rotate]" not in res.stdout
