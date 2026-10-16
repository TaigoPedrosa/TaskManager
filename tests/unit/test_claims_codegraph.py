import json
import os
import shutil
import sqlite3
import subprocess
import sys
import threading
from collections.abc import Callable
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest
import yaml
from click.testing import Result
from lifecycle_estate import add, gated, git, make_estate, on_branch, push_main
from typer.testing import CliRunner

from taskmanager.cli.main import app as cli_app
from taskmanager.core.enums import NodeKind
from taskmanager.core.status import Action, Outcome, Status
from taskmanager.engine import claims as claims_module
from taskmanager.engine.claims import Claims, codegraph_lines
from taskmanager.engine.doctor import CODEGRAPH_INSTALL

# Stands in for the codegraph CLI. It logs each call, answers from the JSON file
# $CODEGRAPH_ANSWERS and, like codegraph, reads the nearest index at or above `-p`.
STUB = """
import json, os, sqlite3, sys, time
from contextlib import closing
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


if cmd == "init":
    wait = answers.get("init_wait")
    while wait and not Path(wait).exists():
        time.sleep(0.01)
    if not answers.get("init_leaves_no_index"):
        target = Path(rest[-1]) / ".codegraph"
        target.mkdir(exist_ok=True)
        with closing(sqlite3.connect(target / "codegraph.db")) as db:
            db.execute("CREATE TABLE marker (source TEXT)")
            db.execute("INSERT INTO marker VALUES ('init')")
            db.commit()
elif cmd == "sync":
    if not (Path(rest[-1]) / ".codegraph" / "codegraph.db").is_file():
        sys.exit("not initialized")
elif cmd == "query":
    here = Path(option("-p")).resolve()
    root = next(d for d in (here, *here.parents) if (d / ".codegraph" / "codegraph.db").is_file())
    print(json.dumps([{"node": {"name": rest[0], "projectPath": str(root)}}]))
elif cmd == "node":
    path = option("-f")
    users = answers.get("users", {}).get(path)
    if users is None:
        users = "no other indexed file depends on it"
    elif isinstance(users, list):
        users = f"used by {len(users)} files: {', '.join(users)}"
    print(f"**{path}** — 1 symbol, {users}\\n\\n**Symbols**")
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


def without_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    git_only = tmp_path / "git-only"
    git_only.mkdir()
    git_bin = shutil.which("git")
    assert git_bin is not None
    (git_only / "git").symlink_to(git_bin)
    monkeypatch.setenv("PATH", str(git_only))


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
    claims = make_estate(tmp_path, config=gated("api"))
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
    claims = make_estate(tmp_path, config=gated("api"))
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
        users={"src/a.py": ["src/b.py", "src/c.py"], "src/lone.py": ["src/c.py"]},
        impact={
            "src/a.py": ["src/a.py"],
            "run": ["src/b.py"],
            "helper": ["src/a.py", "src/b.py"],
            "Box": ["src/a.py"],
            "Box.open": ["src/a.py", "src/b.py", "src/c.py"],
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
    assert f"impact alone --depth 1 --json -p {worktree}" not in calls(log)
    assert not [c for c in calls(log) if c.startswith("impact src/")]


def test_a_claim_skips_a_declared_file_no_other_indexed_file_depends_on(
    tmp_path: Path, log: Path, answer: Answer
) -> None:
    claims = make_estate(tmp_path)
    indexed(claims.root / "api")
    add(claims, "T2", files=["src/b.py"])
    add(claims, "T1", files=["src/a.py"])
    claim(claims, "T2")
    answer(symbols={"src/a.py": [["run", "function"]]}, impact={"run": ["src/b.py"]})

    lines, worktree = claim(claims, "T1")

    assert lines == [f"ready {worktree}"]
    assert not [c for c in calls(log) if c.startswith("impact")]


@pytest.mark.parametrize(
    "users",
    [
        f"used by 9 files: {', '.join(f'src/c{i}.py' for i in range(1, 9))}, +1 more",
        "imported by src/b.py",
    ],
)
def test_a_claim_asks_impact_of_each_symbol_when_the_listing_names_only_some_dependents(
    tmp_path: Path, log: Path, answer: Answer, users: str
) -> None:
    claims = make_estate(tmp_path)
    indexed(claims.root / "api")
    add(claims, "T2", files=["src/b.py"])
    add(claims, "T1", files=["src/a.py"])
    claim(claims, "T2")
    answer(
        symbols={"src/a.py": [["run", "function"]]},
        users={"src/a.py": users},
        impact={"run": ["src/b.py"]},
    )

    lines, worktree = claim(claims, "T1")

    assert lines == [f"ready {worktree}", "run reaches src/b.py held by T2"]


def test_a_container_claim_reads_each_repository_s_declared_files_in_that_repository_s_worktree(
    tmp_path: Path, log: Path
) -> None:
    claims = make_estate(tmp_path, repos=("api", "web"), config=gated("api", "web"))
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
    assert [c for c in calls(log) if c.startswith("node")] == [
        f"node -f src/a.py --symbols-only -p {api}",
        f"node -f src/w.py --symbols-only -p {web}",
    ]


def test_a_claim_with_no_index_and_no_codegraph_cli_prints_no_codegraph_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    claims = make_estate(tmp_path, config=gated("api"))
    add(claims, "T1", files=["src/a.py"])
    without_cli(tmp_path, monkeypatch)

    lines, worktree = claim(claims, "T1")

    assert lines == []
    assert not (worktree / ".codegraph").exists()


def test_a_claim_on_a_repository_with_no_index_inits_it_then_seeds_the_worktree_from_it(
    tmp_path: Path, log: Path
) -> None:
    claims = make_estate(tmp_path, config=gated("api"))
    checkout = claims.root / "api"
    add(claims, "T1", files=["src/a.py"])

    lines, worktree = claim(claims, "T1")

    assert lines == [f"ready {worktree}"]
    assert calls(log)[0] == f"init -y {checkout}"
    assert calls(log)[1] == f"sync --quiet {worktree}"
    for tree in (checkout, worktree):
        with closing(sqlite3.connect(tree / ".codegraph" / "codegraph.db")) as db:
            assert db.execute("SELECT source FROM marker").fetchall() == [("init",)]
    assert (claims.root / ".taskmanager" / "locks" / "codegraph-api.lock").is_file()


def test_a_claim_on_an_indexed_repository_runs_no_init_and_takes_no_lock(
    tmp_path: Path, log: Path
) -> None:
    claims = make_estate(tmp_path, config=gated("api"))
    indexed(claims.root / "api")
    add(claims, "T1", files=["src/a.py"])

    claim(claims, "T1")

    assert not [c for c in calls(log) if c.startswith("init")]
    assert not (claims.root / ".taskmanager" / "locks").exists()


def test_a_claim_whose_init_fails_stands_with_one_unavailable_line(
    tmp_path: Path, answer: Answer
) -> None:
    claims = make_estate(tmp_path, config=gated("api"))
    add(claims, "T1", files=["src/a.py"])
    answer(fails="init")

    lines, worktree = claim(claims, "T1")

    assert lines == ["unavailable (codegraph init failed with exit code 1: init broke)"]
    assert not (worktree / ".codegraph").exists()


def test_a_claim_whose_init_runs_past_its_timeout_stands_with_one_unavailable_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, answer: Answer
) -> None:
    claims = make_estate(tmp_path, config=gated("api"))
    add(claims, "T1", files=["src/a.py"])
    answer(sleeps="init")
    monkeypatch.setattr(claims_module, "CODEGRAPH_INIT_TIMEOUT", 0.5)
    monkeypatch.setattr(claims_module, "CODEGRAPH_TIMEOUT", 600)

    lines, _ = claim(claims, "T1")

    assert len(lines) == 1
    assert lines[0].startswith("unavailable (codegraph init: ")
    assert "timed out after 0.5 seconds" in lines[0]


def test_the_init_timeout_is_separate_from_and_longer_than_the_call_timeout() -> None:
    assert claims_module.CODEGRAPH_INIT_TIMEOUT > claims_module.CODEGRAPH_TIMEOUT


def test_a_claim_whose_init_leaves_no_index_stands_with_one_unavailable_line(
    tmp_path: Path, answer: Answer
) -> None:
    claims = make_estate(tmp_path, config=gated("api"))
    add(claims, "T1", files=["src/a.py"])
    answer(init_leaves_no_index=True)

    lines, _ = claim(claims, "T1")

    assert lines == [f"unavailable (codegraph init left no index in {claims.root / 'api'})"]


def test_two_claims_racing_on_a_repository_with_no_index_run_init_once_and_both_seed(
    tmp_path: Path, log: Path, answer: Answer, monkeypatch: pytest.MonkeyPatch
) -> None:
    claims = make_estate(tmp_path, config=gated("api"))
    checkout = claims.root / "api"
    release = tmp_path / "release"
    answer(init_wait=str(release))
    first_in_init, second_checked = threading.Event(), threading.Event()
    real_call, real_index = claims_module._codegraph, claims_module.codegraph_index

    def call(*args: str, **kwargs: Any) -> str:
        if args[0] == "init":
            first_in_init.set()
        return real_call(*args, **kwargs)

    def index(repo: Path) -> Path:
        if threading.current_thread().name == "second":
            second_checked.set()
        return real_index(repo)

    monkeypatch.setattr(claims_module, "_codegraph", call)
    monkeypatch.setattr(claims_module, "codegraph_index", index)
    seen: dict[str, list[str]] = {}
    worktrees = {name: tmp_path / name for name in ("first", "second")}

    def run(name: str) -> None:
        worktrees[name].mkdir()
        seen[name] = codegraph_lines(checkout, worktrees[name], [], {}, tmp_path / "lock")

    first = threading.Thread(target=run, args=("first",), name="first")
    second = threading.Thread(target=run, args=("second",), name="second")
    first.start()
    assert first_in_init.wait(30)
    second.start()
    assert second_checked.wait(30)
    release.touch()
    for thread in (first, second):
        thread.join(30)
        assert not thread.is_alive()

    assert [c for c in calls(log) if c.startswith("init")] == [f"init -y {checkout}"]
    assert seen == {name: [f"ready {path}"] for name, path in worktrees.items()}
    for path in worktrees.values():
        with closing(sqlite3.connect(path / ".codegraph" / "codegraph.db")) as db:
            assert db.execute("SELECT source FROM marker").fetchall() == [("init",)]


def test_a_claim_whose_codegraph_directory_holds_no_database_inits_the_repository(
    tmp_path: Path, log: Path
) -> None:
    claims = make_estate(tmp_path, config=gated("api"))
    checkout = claims.root / "api"
    (checkout / ".codegraph").mkdir()
    (checkout / ".codegraph" / ".gitignore").write_text("*\n!.gitignore\n", encoding="utf-8")
    add(claims, "T1", files=["src/a.py"])

    lines, worktree = claim(claims, "T1")

    assert lines == [f"ready {worktree}"]
    assert calls(log)[0] == f"init -y {checkout}"


def test_a_claim_without_the_codegraph_cli_stands_with_one_unavailable_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    claims = make_estate(tmp_path, config=gated("api"))
    indexed(claims.root / "api")
    add(claims, "T1", files=["src/a.py"])
    without_cli(tmp_path, monkeypatch)

    lines, _ = claim(claims, "T1")

    assert lines == [f"unavailable (codegraph is not on PATH; install it: `{CODEGRAPH_INSTALL}`)"]


def test_a_claim_whose_sync_fails_stands_with_one_unavailable_line(
    tmp_path: Path, answer: Answer
) -> None:
    claims = make_estate(tmp_path, config=gated("api"))
    indexed(claims.root / "api")
    add(claims, "T1", files=["src/a.py"])
    answer(fails="sync")

    lines, _ = claim(claims, "T1")

    assert lines == ["unavailable (codegraph sync failed with exit code 1: sync broke)"]


def test_a_claim_whose_index_cannot_be_copied_stands_with_one_unavailable_line(
    tmp_path: Path, log: Path
) -> None:
    claims = make_estate(tmp_path, config=gated("api"))
    index = indexed(claims.root / "api")
    index.write_bytes(b"not a database, though long enough to be read as one" * 4)
    add(claims, "T1", files=["src/a.py"])

    lines, _ = claim(claims, "T1")

    assert lines == [f"unavailable (copying {index}: file is not a database)"]
    assert calls(log) == []


def test_a_codegraph_call_past_its_timeout_leaves_the_claim_standing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, answer: Answer
) -> None:
    claims = make_estate(tmp_path, config=gated("api"))
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
    claims = make_estate(tmp_path, config=gated("api"))
    indexed(claims.root / "api")
    add(claims, "T2", files=["src/b.py"])
    add(claims, "T1", files=["src/a.py"])
    claim(claims, "T2")
    answer(
        symbols={"src/a.py": [["run", "function"]]},
        users={"src/a.py": ["src/b.py"]},
        impact={"run": {"symbol": "run"}},
    )

    lines, worktree = claim(claims, "T1")

    assert lines == [
        f"ready {worktree}",
        "unavailable (codegraph impact run printed no affected files)",
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
    claims = make_estate(tmp_path, config=gated("api"))
    indexed(claims.root / "api")
    add(claims, "T2", files=["src/b.py"])
    add(claims, "T1", files=["src/a.py"])
    claim(claims, "T2")
    answer(
        symbols={"src/a.py": [["run", "function"]]},
        users={"src/a.py": ["src/b.py"]},
        impact={"run": ["src/b.py"]},
    )

    claimed = yaml.safe_load(start(claims, "T1").output)

    assert claimed["codegraph"] == [
        f"ready {claimed['worktree']}",
        "run reaches src/b.py held by T2",
    ]


def test_task_start_json_with_codegraph_lines_is_one_document_on_stdout(
    tmp_path: Path, log: Path
) -> None:
    claims = make_estate(tmp_path, config=gated("api"))
    indexed(claims.root / "api")
    add(claims, "T1", files=["src/a.py"])

    res = start(claims, "T1", "--json")

    assert res.stderr == ""
    claimed = json.loads(res.output)
    assert claimed["codegraph"] == [f"ready {claimed['worktree']}"]


def test_task_start_with_no_index_and_no_codegraph_cli_prints_no_codegraph_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    claims = make_estate(tmp_path, config=gated("api"))
    add(claims, "T1", files=["src/a.py"])
    without_cli(tmp_path, monkeypatch)

    claimed = yaml.safe_load(start(claims, "T1").output)

    assert claimed["worktree"] is not None
    assert "codegraph" not in claimed


def review_claim(claims: Claims, node_id: str = "T1") -> tuple[list[str], str]:
    result = claims.start(node_id, "reviewer", "s1")
    assert result.action == Action.REVIEW
    assert result.branch is not None
    return result.codegraph, result.branch


def reviewable(claims: Claims) -> tuple[Path, str]:
    """T1 awaiting review, its branch holding a file the checkout's working tree lacks."""
    repo = claims.root / "api"
    add(claims, "T1", status=Status.IMPLEMENTED, review=True, files=["src/a.py"])
    sha = on_branch(repo, claims.branch_of("T1"), "src/a.py", "from the branch\n")
    return repo, sha


def cached(claims: Claims, sha: str) -> Path:
    return claims.root / ".taskmanager" / "cache" / "codegraph" / sha


def test_a_review_claim_prints_ready_for_an_index_of_the_commit_under_review(
    tmp_path: Path, log: Path
) -> None:
    claims = make_estate(tmp_path, config=gated("api"))
    repo, sha = reviewable(claims)

    lines, _ = review_claim(claims)

    tree = cached(claims, sha)
    assert lines == [f"ready {tree}"]
    assert (tree / "src" / "a.py").read_text(encoding="utf-8") == "from the branch\n"
    assert not (repo / "src" / "a.py").exists()
    assert (tree / ".codegraph" / "codegraph.db").is_file()
    assert not (repo / ".codegraph").exists()


def test_a_review_claim_reuses_the_cached_index_of_a_commit_a_verification_indexed(
    tmp_path: Path, log: Path
) -> None:
    claims = make_estate(tmp_path, config=gated("api"))
    _, sha = reviewable(claims)
    review_claim(claims)
    claims.runtime.release_lease("T1")
    inits = [c for c in calls(log) if c.startswith("init")]

    lines, _ = review_claim(claims)

    assert lines == [f"ready {cached(claims, sha)}"]
    assert [c for c in calls(log) if c.startswith("init")] == inits


def test_a_review_claim_without_the_codegraph_cli_prints_no_codegraph_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    claims = make_estate(tmp_path, config=gated("api"))
    reviewable(claims)
    without_cli(tmp_path, monkeypatch)

    lines, _ = review_claim(claims)

    assert lines == []


def test_a_review_claim_whose_index_fails_stands_with_one_unavailable_line(
    tmp_path: Path, answer: Answer
) -> None:
    claims = make_estate(tmp_path, config=gated("api"))
    reviewable(claims)
    answer(fails="init")

    lines, _ = review_claim(claims)

    assert len(lines) == 1
    assert lines[0].startswith("unavailable (`codegraph init` failed with exit code 1: init broke")


def test_a_review_claim_whose_index_runs_past_the_init_timeout_stands_with_one_unavailable_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, answer: Answer
) -> None:
    claims = make_estate(tmp_path, config=gated("api"))
    reviewable(claims)
    answer(sleeps="init")
    monkeypatch.setattr(claims_module, "CODEGRAPH_INIT_TIMEOUT", 0.5)

    lines, _ = review_claim(claims)

    assert lines == ["unavailable (`codegraph init` timed out after 0.5 seconds)"]


def test_a_review_claim_whose_branch_names_no_commit_stands_with_one_unavailable_line(
    tmp_path: Path, log: Path
) -> None:
    claims = make_estate(tmp_path, config=gated("api"))
    add(claims, "T1", status=Status.IMPLEMENTED, review=True, files=["src/a.py"])

    lines, branch = review_claim(claims)

    assert lines == [f"unavailable ({branch} names no commit in api)"]
    assert calls(log) == []


def test_a_review_claim_whose_cache_cannot_be_written_stands_with_one_unavailable_line(
    tmp_path: Path, log: Path
) -> None:
    claims = make_estate(tmp_path, config=gated("api"))
    reviewable(claims)
    (claims.root / ".taskmanager" / "cache").write_text("a file, not a directory", encoding="utf-8")

    lines, _ = review_claim(claims)

    assert len(lines) == 1
    assert lines[0].startswith("unavailable (")


def test_a_container_review_claim_prints_one_ready_line_per_repository(
    tmp_path: Path, log: Path
) -> None:
    claims = make_estate(tmp_path, repos=("api", "web"), config=gated("api", "web"))
    push_main(claims.root / "web", "web.txt", "web\n")
    add(claims, "S", NodeKind.SPEC)
    add(claims, "P", NodeKind.PLAN, parent="S", review=True, status=Status.LANDED)
    add(claims, "A", parent="P", repo="api", review=False, fix=False, status=Status.COMPLETED)
    add(claims, "W", parent="P", repo="web", review=False, fix=False, status=Status.COMPLETED)
    shas = [git(claims.root / repo, "rev-parse", "origin/main") for repo in ("api", "web")]

    lines, _ = review_claim(claims, "P")

    assert shas[0] != shas[1]
    assert lines == [f"ready {cached(claims, sha)}" for sha in shas]
