import json
import os
import shutil
import sqlite3
import subprocess
import sys
from collections.abc import Callable
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest
import yaml
from click.testing import Result
from lifecycle_estate import add, make_estate
from typer.testing import CliRunner

from taskmanager.cli.main import app as cli_app
from taskmanager.core.enums import NodeKind
from taskmanager.core.status import Action, Outcome, Status
from taskmanager.engine import claims as claims_module
from taskmanager.engine.claims import Claims
from taskmanager.engine.doctor import CODEGRAPH_INSTALL

# Stands in for the codegraph CLI. It logs each call, answers from the JSON file
# $CODEGRAPH_ANSWERS and, like codegraph, reads the nearest index at or above `-p`.
STUB = """
import json, os, sys, time
from pathlib import Path

args = sys.argv[1:]
with open(os.environ["CODEGRAPH_LOG"], "a") as log:
    log.write(" ".join(args) + "\\n")
answers = json.loads(Path(os.environ["CODEGRAPH_ANSWERS"]).read_text())
cmd, rest = args[0], args[1:]
if answers.get("sleeps") == cmd:
    time.sleep(30)
if answers.get("fails") == cmd:
    sys.exit(f"{cmd} broke")


def option(flag):
    return rest[rest.index(flag) + 1]


if cmd == "sync":
    if not (Path(rest[-1]) / ".codegraph" / "codegraph.db").is_file():
        sys.exit("not initialized")
elif cmd == "query":
    here = Path(option("-p")).resolve()
    root = next(d for d in (here, *here.parents) if (d / ".codegraph" / "codegraph.db").is_file())
    print(json.dumps([{"node": {"name": rest[0], "projectPath": str(root)}}]))
elif cmd == "node":
    path = option("-f")
    print(f"**{path}**\\n\\n**Symbols**")
    for name, kind in answers.get("symbols", {}).get(path, []):
        print(f"- `{name}` ({kind}) () — :1")
elif cmd == "impact":
    found = answers.get("impact", {}).get(rest[0])
    if found is None:
        print(f'Symbol "{rest[0]}" not found')
    elif isinstance(found, list):
        print(json.dumps({"symbol": rest[0], "affected": [{"filePath": f} for f in found]}))
    else:
        print(json.dumps(found))
"""

Answer = Callable[..., None]


@pytest.fixture
def log(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "codegraph"
    stub.write_text(f"#!{sys.executable}\n{STUB}", encoding="utf-8")
    stub.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("CODEGRAPH_LOG", str(tmp_path / "codegraph.log"))
    monkeypatch.setenv("CODEGRAPH_ANSWERS", str(tmp_path / "answers.json"))
    (tmp_path / "answers.json").write_text("{}", encoding="utf-8")
    return tmp_path / "codegraph.log"


@pytest.fixture
def answer(tmp_path: Path, log: Path) -> Answer:
    def write(**answers: Any) -> None:
        (tmp_path / "answers.json").write_text(json.dumps(answers), encoding="utf-8")

    return write


def calls(log: Path) -> list[str]:
    return log.read_text(encoding="utf-8").splitlines() if log.exists() else []


def indexed(checkout: Path) -> Path:
    """A codegraph index in `checkout`, holding a row only a copy of it carries."""
    (checkout / ".codegraph").mkdir()
    index = checkout / ".codegraph" / "codegraph.db"
    with closing(sqlite3.connect(index)) as db:
        db.execute("CREATE TABLE marker (source TEXT)")
        db.execute("INSERT INTO marker VALUES ('checkout')")
        db.commit()
    return index


def claim(claims: Claims, node_id: str) -> tuple[list[str], Path]:
    """Claims `node_id` with its worktree cut inside the checkout, where codegraph would
    otherwise answer from the checkout's index."""
    result = claims.start(node_id, "agent-1", "s1", worktree_dir=claims.root / "api" / ".wt")
    assert result.action == Action.IMPLEMENT
    assert result.worktree is not None
    assert claims.runtime.get_lease(node_id) is not None
    return result.codegraph, Path(result.worktree)


def test_a_claim_seeds_its_worktree_index_from_the_checkout_and_queries_there_answer_for_it(
    tmp_path: Path, log: Path
) -> None:
    claims = make_estate(tmp_path)
    indexed(claims.root / "api")
    add(claims, "T1", files=["src/a.py"])

    lines, worktree = claim(claims, "T1")

    assert lines == [f"ready {worktree}"]
    with closing(sqlite3.connect(worktree / ".codegraph" / "codegraph.db")) as db:
        assert db.execute("SELECT source FROM marker").fetchall() == [("checkout",)]
    assert f"sync --quiet {worktree}" in calls(log)
    assert not [c for c in calls(log) if c.startswith("init")]
    query = subprocess.run(
        ["codegraph", "query", "helper", "--json", "-p", str(worktree)],
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(query.stdout)[0]["node"]["projectPath"] == str(worktree.resolve())


def test_a_claim_names_each_declared_symbol_whose_dependents_sit_in_a_file_another_lease_holds(
    tmp_path: Path, log: Path, answer: Answer
) -> None:
    claims = make_estate(tmp_path)
    indexed(claims.root / "api")
    add(claims, "T2", files=["src/b.py"])
    add(claims, "T1", files=["src/a.py", "src/lone.py", "src/new.py"])
    claim(claims, "T2")
    answer(
        symbols={
            "src/a.py": [
                ["run", "method"],
                ["helper", "function"],
                ["Box", "class"],
                ["open", "method"],
                ["helper", "function"],
            ],
            "src/lone.py": [["alone", "function"]],
        },
        impact={
            "src/a.py": ["src/a.py", "src/b.py"],
            "run": ["src/b.py"],
            "helper": ["src/a.py", "src/b.py"],
            "Box": ["src/a.py"],
            "Box.open": ["src/a.py", "src/b.py", "src/c.py"],
            "src/lone.py": ["src/lone.py", "src/c.py"],
            "alone": ["src/b.py"],
        },
    )

    lines, worktree = claim(claims, "T1")

    assert lines == [
        f"ready {worktree}",
        "run reaches src/b.py held by T2",
        "helper reaches src/b.py held by T2",
        "Box.open reaches src/b.py held by T2",
    ]
    assert f"node -f src/lone.py --symbols-only -p {worktree}" not in calls(log)


def test_a_container_claim_reads_each_repository_s_declared_files_in_that_repository_s_worktree(
    tmp_path: Path, log: Path
) -> None:
    claims = make_estate(tmp_path, repos=("api", "web"))
    for repo in ("api", "web"):
        indexed(claims.root / repo)
    add(
        claims,
        "P",
        NodeKind.PLAN,
        review=True,
        fix=True,
        status=Status.REVIEWED,
        outcome=Outcome.REJECT,
        review_cycles=1,
    )
    add(claims, "A", parent="P", repo="api", status=Status.COMPLETED, files=["src/a.py"])
    add(claims, "W", parent="P", repo="web", status=Status.COMPLETED, files=["src/w.py"])

    result = claims.start("P", "fixer", "s1")

    assert result.action == Action.FIX
    api, web = result.worktrees["api"], result.worktrees["web"]
    assert result.codegraph == [f"ready {api}", f"ready {web}"]
    assert [c for c in calls(log) if c.startswith("impact")] == [
        f"impact src/a.py --depth 1 --json -p {api}",
        f"impact src/w.py --depth 1 --json -p {web}",
    ]


def test_a_claim_with_no_codegraph_directory_prints_no_codegraph_line(
    tmp_path: Path, log: Path
) -> None:
    claims = make_estate(tmp_path)
    add(claims, "T1", files=["src/a.py"])

    lines, worktree = claim(claims, "T1")

    assert lines == []
    assert not (worktree / ".codegraph").exists()
    assert calls(log) == []


def test_a_claim_without_the_codegraph_cli_stands_with_one_unavailable_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    claims = make_estate(tmp_path)
    indexed(claims.root / "api")
    add(claims, "T1", files=["src/a.py"])
    git_only = tmp_path / "git-only"
    git_only.mkdir()
    git = shutil.which("git")
    assert git is not None
    (git_only / "git").symlink_to(git)
    monkeypatch.setenv("PATH", str(git_only))

    lines, _ = claim(claims, "T1")

    assert lines == [f"unavailable (codegraph is not on PATH; install it: `{CODEGRAPH_INSTALL}`)"]


def test_a_claim_whose_checkout_has_no_index_stands_with_one_unavailable_line(
    tmp_path: Path, log: Path
) -> None:
    claims = make_estate(tmp_path)
    checkout = claims.root / "api"
    (checkout / ".codegraph").mkdir()
    add(claims, "T1", files=["src/a.py"])

    lines, _ = claim(claims, "T1")

    assert lines == [
        f"unavailable ({checkout} has no codegraph index; run `codegraph init {checkout}`)"
    ]
    assert calls(log) == []


def test_a_claim_whose_sync_fails_stands_with_one_unavailable_line(
    tmp_path: Path, answer: Answer
) -> None:
    claims = make_estate(tmp_path)
    indexed(claims.root / "api")
    add(claims, "T1", files=["src/a.py"])
    answer(fails="sync")

    lines, _ = claim(claims, "T1")

    assert lines == ["unavailable (codegraph sync failed with exit code 1: sync broke)"]


def test_a_claim_whose_index_cannot_be_copied_stands_with_one_unavailable_line(
    tmp_path: Path, log: Path
) -> None:
    claims = make_estate(tmp_path)
    index = indexed(claims.root / "api")
    index.write_bytes(b"not a database, though long enough to be read as one" * 4)
    add(claims, "T1", files=["src/a.py"])

    lines, _ = claim(claims, "T1")

    assert lines == [f"unavailable (copying {index}: file is not a database)"]
    assert calls(log) == []


def test_a_codegraph_call_past_its_timeout_leaves_the_claim_standing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, answer: Answer
) -> None:
    claims = make_estate(tmp_path)
    indexed(claims.root / "api")
    add(claims, "T1", files=["src/a.py"])
    answer(sleeps="sync")
    monkeypatch.setattr(claims_module, "CODEGRAPH_TIMEOUT", 0.5)

    lines, _ = claim(claims, "T1")

    assert len(lines) == 1
    assert lines[0].startswith("unavailable (codegraph sync: ")
    assert "timed out after 0.5 seconds" in lines[0]


def test_an_advisory_that_cannot_read_impact_keeps_the_ready_line_and_adds_one_unavailable(
    tmp_path: Path, answer: Answer
) -> None:
    claims = make_estate(tmp_path)
    indexed(claims.root / "api")
    add(claims, "T1", files=["src/a.py"])
    answer(impact={"src/a.py": {"symbol": "src/a.py"}})

    lines, worktree = claim(claims, "T1")

    assert lines == [
        f"ready {worktree}",
        "unavailable (codegraph impact src/a.py printed no affected files)",
    ]


def start(claims: Claims, node_id: str, *flags: str) -> Result:
    res = CliRunner().invoke(
        cli_app,
        [
            "task",
            "start",
            node_id,
            "--agent",
            "agent-1",
            "--session",
            "s1",
            "--worktree-dir",
            str(claims.root / "api" / ".wt"),
            *flags,
            "-C",
            str(claims.root),
        ],
    )
    assert res.exit_code == 0, res.output
    return res


def test_task_start_prints_the_codegraph_lines_in_its_yaml_document(
    tmp_path: Path, log: Path, answer: Answer
) -> None:
    claims = make_estate(tmp_path)
    indexed(claims.root / "api")
    add(claims, "T2", files=["src/b.py"])
    add(claims, "T1", files=["src/a.py"])
    claim(claims, "T2")
    answer(
        symbols={"src/a.py": [["run", "function"]]},
        impact={"src/a.py": ["src/b.py"], "run": ["src/b.py"]},
    )

    claimed = yaml.safe_load(start(claims, "T1").output)

    assert claimed["codegraph"] == [
        f"ready {claimed['worktree']}",
        "run reaches src/b.py held by T2",
    ]


def test_task_start_json_with_codegraph_lines_is_one_document_on_stdout(
    tmp_path: Path, log: Path
) -> None:
    claims = make_estate(tmp_path)
    indexed(claims.root / "api")
    add(claims, "T1", files=["src/a.py"])

    res = start(claims, "T1", "--json")

    assert res.stderr == ""
    claimed = json.loads(res.output)
    assert claimed["codegraph"] == [f"ready {claimed['worktree']}"]


def test_task_start_with_no_codegraph_directory_prints_no_codegraph_key(
    tmp_path: Path, log: Path
) -> None:
    claims = make_estate(tmp_path)
    add(claims, "T1", files=["src/a.py"])

    claimed = yaml.safe_load(start(claims, "T1").output)

    assert claimed["worktree"] is not None
    assert "codegraph" not in claimed
