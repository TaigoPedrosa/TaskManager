import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from taskmanager.cli.main import app
from taskmanager.db.connection import DatabaseManager
from taskmanager.db.node_repo import NodeRepository
from taskmanager.web.app import create_app

runner = CliRunner()


def tm(root: Path, *args: str) -> tuple[int, str]:
    result = runner.invoke(app, [*args, "-C", str(root)])
    return result.exit_code, result.output


def git_init(path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(path)], check=True)


@pytest.fixture
def root(tmp_path: Path) -> Path:
    """A clone that `tm init` ran in, as the README's quickstart sets one up, with one more
    repository cloned under it."""
    project = tmp_path / "project"
    git_init(project)
    git_init(project / "api")
    assert tm(project, "init")[0] == 0
    return project


def import_task(root: Path, repo: str) -> tuple[int, str]:
    doc = root.parent / "plan.json"
    task = {"id": "P-T", "title": "t", "target_repo": repo}
    doc.write_text(json.dumps({"plans": [{"id": "P", "title": "P", "tasks": [task]}]}))
    return tm(root, "import", "-f", str(doc))


def stored_repo(root: Path, node_id: str) -> str | None:
    node = NodeRepository(DatabaseManager(root / ".taskmanager")).get_node(node_id)
    return None if node is None else node.target_repo


def refusal(repo: str, root: Path) -> str:
    return (
        f"P-T: target_repo '{repo}' is not a git working tree under {root}; name one relative "
        "to it, `.` for the root itself (repositories there: ., api)"
    )


@pytest.mark.parametrize("repo", [".", "api"])
def test_import_takes_the_root_and_a_clone_under_it(root: Path, repo: str) -> None:
    code, output = import_task(root, repo)

    assert code == 0, output
    assert stored_repo(root, "P-T") == repo


@pytest.mark.parametrize("repo", ["apj", "../outside"])
def test_import_refuses_a_target_repo_that_is_not_a_clone_under_the_root(
    root: Path, repo: str
) -> None:
    git_init(root.parent / "outside")

    code, output = import_task(root, repo)

    assert code == 1
    assert refusal(repo, root) in " ".join(output.split())
    assert stored_repo(root, "P-T") is None


def test_task_update_refuses_a_target_repo_that_is_not_a_clone(root: Path) -> None:
    assert import_task(root, "api")[0] == 0

    code, output = tm(root, "task", "update", "P-T", "--repo", "apj")

    assert code == 1
    assert refusal("apj", root) in " ".join(output.split())
    assert stored_repo(root, "P-T") == "api"


def test_the_web_refuses_a_target_repo_that_is_not_a_clone(root: Path) -> None:
    assert import_task(root, "api")[0] == 0

    res = TestClient(create_app(root)).patch("/api/nodes/P-T", json={"target_repo": "apj"})

    assert res.status_code == 400
    assert refusal("apj", root) in res.text
    assert stored_repo(root, "P-T") == "api"


def test_a_stored_target_repo_whose_clone_is_gone_blocks_no_other_write(root: Path) -> None:
    git_init(root / "web")
    assert import_task(root, "web")[0] == 0
    shutil.rmtree(root / "web")

    code, output = tm(root, "task", "update", "P-T", "--title", "renamed")

    assert code == 0, output


def discovered(root: Path) -> dict[str, Any]:
    code, output = tm(root, "wave", "discover", "--session", "s", "--slots", "5")
    assert code == 0, output
    data: dict[str, Any] = json.loads(output.splitlines()[0])
    return data


def test_discovery_holds_a_node_whose_repo_has_no_main_gate_naming_the_key(root: Path) -> None:
    assert import_task(root, ".")[0] == 0
    held = (
        "P-T: repos...gates.main is not configured, so nothing lands in .; set it with "
        '`tm config set repos...gates.main.command "<your test command>"`'
    )

    data = discovered(root)

    assert data["chosen"] == []
    assert held in data["held"]
    waves = TestClient(create_app(root)).get("/api/waves", params={"size": 5}).json()["waves"]
    assert waves[0]["entries"] == []
    assert held in waves[0]["held"]

    assert tm(root, "config", "set", "repos...gates.main.command", "true")[0] == 0

    assert [entry["id"] for entry in discovered(root)["chosen"]] == ["P-T"]
