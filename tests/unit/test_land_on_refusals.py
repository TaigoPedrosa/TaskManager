"""Moving a spec's `land_on` across a dependency edge is refused only while work on that edge
still has to land: an edge whose either end is finished never blocks the move, on any route."""

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from taskmanager.cli.main import app
from taskmanager.db.connection import DatabaseManager
from taskmanager.db.node_repo import NodeRepository
from taskmanager.web.app import create_app

TARGET = "release/x"


def tm(root: Path, *args: str, stdin: str | None = None) -> tuple[int, str]:
    result = CliRunner().invoke(app, [*args, "-C", str(root)], input=stdin)
    return result.exit_code, " ".join(result.output.split())


def spec(spec_id: str, tasks: list[dict[str, Any]]) -> str:
    plan = {"id": f"{spec_id}-P", "title": "P", "tasks": tasks}
    return json.dumps({"spec": {"id": spec_id, "title": spec_id}, "plans": [plan]})


def task(task_id: str, status: str, *depends_on: str) -> dict[str, Any]:
    return {"id": task_id, "title": task_id, "status": status, "depends_on": list(depends_on)}


def estate(root: Path, waiter: str, dep: str) -> Path:
    """A and W both land on main, W-P-w waiting on A-P-a, each at the status given."""
    assert tm(root, "init")[0] == 0
    for document in (
        spec("A", [task("A-P-a", dep)]),
        spec("W", [task("W-P-w", waiter, "A-P-a")]),
    ):
        code, output = tm(root, "import", stdin=document)
        assert code == 0, output
    return root


def by_cli(root: Path) -> tuple[bool, str]:
    code, output = tm(root, "task", "update", "W", "--set", f"land_on={TARGET}")
    return code == 0, output


def by_import(root: Path) -> tuple[bool, str]:
    head = {"id": "W", "title": "W", "frontmatter": {"land_on": TARGET}}
    code, output = tm(root, "import", stdin=json.dumps({"spec": head}))
    return code == 0, output


def by_web(root: Path) -> tuple[bool, str]:
    response = TestClient(create_app(root)).patch("/api/nodes/W", json={"land_on": TARGET})
    return response.is_success, response.text


ROUTES: dict[str, Callable[[Path], tuple[bool, str]]] = {
    "cli": by_cli,
    "import": by_import,
    "web": by_web,
}


def land_on(root: Path) -> object:
    node = NodeRepository(DatabaseManager(root / ".taskmanager")).get_node("W")
    assert node is not None
    return node.frontmatter.get("land_on")


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize(
    ("waiter", "dep"),
    [
        ("COMPLETED", "COMPLETED"),
        ("COMPLETED", "READY"),
        ("SUPERSEDED", "READY"),
        ("READY", "COMPLETED"),
    ],
    ids=["both-completed", "waiter-completed", "waiter-superseded", "dep-completed"],
)
def test_moving_land_on_across_an_edge_with_a_finished_end_is_accepted(
    tmp_path: Path, route: str, waiter: str, dep: str
) -> None:
    root = estate(tmp_path, waiter, dep)

    accepted, output = ROUTES[route](root)

    assert accepted, output
    assert land_on(root) == TARGET


@pytest.mark.parametrize("route", ROUTES)
def test_moving_land_on_across_an_edge_whose_ends_still_land_is_refused(
    tmp_path: Path, route: str
) -> None:
    root = estate(tmp_path, "READY", "READY")

    accepted, output = ROUTES[route](root)

    assert not accepted
    assert (
        f"W-P-w: depends on A-P-a, which lands on main, but W-P-w lands on {TARGET}; remove "
        "the edge, or land both on one target"
    ) in " ".join(output.split())
    assert land_on(root) is None


@pytest.mark.parametrize(("dep", "accepted"), [("COMPLETED", True), ("READY", False)])
def test_an_edge_drawn_to_another_target_is_refused_only_while_its_dep_still_lands(
    tmp_path: Path, dep: str, accepted: bool
) -> None:
    root = estate(tmp_path, "READY", dep)
    assert tm(root, "task", "depends", "W-P-w", "--remove", "A-P-a")[0] == 0
    assert by_cli(root)[0]

    code, output = tm(root, "task", "depends", "W-P-w", "--add", "A-P-a")

    assert (code == 0) is accepted, output
