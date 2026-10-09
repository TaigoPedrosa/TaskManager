"""`/api/waves`' wave 1 and `tm wave discover` read the same graph rules, so their `chosen`
must agree exactly -- including under a live condition and a container's repository order."""

import json
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from taskmanager.cli.main import app as cli_app
from taskmanager.core.status import ConditionStage, Status
from taskmanager.db.connection import DatabaseManager
from taskmanager.db.node_repo import NodeRepository
from taskmanager.di.container import create_container
from taskmanager.engine.config import ConfigStore
from taskmanager.engine.operations import Operations
from taskmanager.web.app import create_app

runner = CliRunner()
Web = tuple[TestClient, Path]


@pytest.fixture
def web(tmp_path: Path) -> Web:
    DatabaseManager(tmp_path / ".taskmanager").init_all()
    return TestClient(create_app(tmp_path)), tmp_path


def _set_status(root: Path, node_id: str, status: Status) -> None:
    node_repo = NodeRepository(DatabaseManager(root / ".taskmanager"))
    node = node_repo.get_node(node_id)
    assert node is not None
    node.status = status
    node_repo.save_node(node)


def test_wave_one_matches_discover_under_a_claim_condition_and_a_repo_order(web: Web) -> None:
    client, root = web
    ops = create_container(root).get(Operations)
    for repo in ("api", "alpha", "zeta"):
        subprocess.run(["git", "init", "-q", str(root / repo)], check=True)
        ConfigStore(root).set(f"repos.{repo}.gates.main.command", "true")

    held_spec = ops.add_spec("Held", slug="H")
    held_plan = ops.add_plan("Held", held_spec, slug="P", review=False, fix=False)
    held_task = ops.add_task("Held", held_plan, slug="T")
    ops.update_node(held_task, repo="api")
    ops.add_condition(held_task, "flag", f"test -f {root / 'never-written'}", ConditionStage.CLAIM)

    merge_spec = ops.add_spec("Merge", slug="M")
    container = ops.add_plan("Merge", merge_spec, slug="Q", review=False, fix=False)
    child_alpha = ops.add_task("Alpha", container, slug="A")
    ops.update_node(child_alpha, repo="alpha")
    child_zeta = ops.add_task("Zeta", container, slug="Z")
    ops.update_node(child_zeta, repo="zeta")
    _set_status(root, child_alpha, Status.COMPLETED)
    _set_status(root, child_zeta, Status.COMPLETED)
    _set_status(root, container, Status.IMPLEMENTED)

    ConfigStore(root).set("repo_order", "[zeta, alpha]")

    res = runner.invoke(
        cli_app,
        [
            "wave",
            "discover",
            "--session",
            "sess",
            "--slots",
            "5",
            "--max-strong",
            "5",
            "--path",
            str(root),
        ],
    )
    assert res.exit_code == 0, res.stdout
    chosen = json.loads(res.stdout.splitlines()[0])["chosen"]

    api_entries = client.get("/api/waves", params={"size": 5}).json()["waves"][0]["entries"]

    assert [c["id"] for c in chosen] == [container]
    assert [c["repos"] for c in chosen] == [["zeta", "alpha"]]
    assert [e["id"] for e in api_entries] == [c["id"] for c in chosen]
    assert [e["action"] for e in api_entries] == [c["action"] for c in chosen]
    assert [e["repos"] for e in api_entries] == [c["repos"] for c in chosen]
