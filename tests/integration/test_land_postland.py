"""A reviewed plan on a scratch estate, driven end to end through `tm import`, `tm task add` and
`POST /api/tasks`: a child added under it lands on its branch with no review of its own unless it
is sensitive, and a child that would land on main unreviewed is refused with its message."""

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from taskmanager.cli.main import app as cli_app
from taskmanager.core.models import Node
from taskmanager.core.status import Merge
from taskmanager.db.connection import DatabaseManager
from taskmanager.db.node_repo import NodeRepository
from taskmanager.web.app import create_app

SPEC = {"id": "S", "title": "Spec"}
REVIEWED_PLAN = {
    "spec": SPEC,
    "plans": [{"id": "S-P", "title": "Plan", "review": True, "fix": True}],
}
MIGRATION = "api/migrations/versions/0002_keys.py"
UNREVIEWED_ON_MAIN = (
    "S-P-c: lands on main with review off, so its code would reach main unreviewed: S-P's "
    "review reads only what lands on its branch; set merge=parent, or turn review on"
)

Create = Callable[[Path, dict[str, Any]], tuple[int, str]]


def tm(root: Path, *args: str, stdin: str | None = None) -> tuple[int, str]:
    result = CliRunner().invoke(cli_app, [*args, "-C", str(root)], input=stdin)
    # Rich wraps a long refusal at the terminal width.
    return result.exit_code, " ".join(result.output.split())


def by_import(root: Path, fields: dict[str, Any]) -> tuple[int, str]:
    plan = {"id": "S-P", "title": "Plan", "tasks": [{"id": "S-P-c", "title": "c", **fields}]}
    return tm(root, "import", stdin=json.dumps({"spec": SPEC, "plans": [plan]}))


def by_task_add(root: Path, fields: dict[str, Any]) -> tuple[int, str]:
    args: list[str] = []
    for key, value in fields.items():
        if key == "frontmatter":
            args += [arg for k, v in value.items() for arg in ("--set", f"{k}={json.dumps(v)}")]
        elif key == "merge":
            args += ["--merge", value]
        else:
            args.append(f"--{key}" if value else f"--no-{key}")
    return tm(root, "task", "add", "c", "--plan", "S-P", "--slug", "c", *args)


def by_api(root: Path, fields: dict[str, Any]) -> tuple[int, str]:
    response = TestClient(create_app(root)).post(
        "/api/tasks", json={"title": "c", "plan": "S-P", "slug": "c", **fields}
    )
    return response.status_code, response.json().get("detail", "")


def child(root: Path) -> Node | None:
    return NodeRepository(DatabaseManager(root / ".taskmanager")).get_node("S-P-c")


@pytest.fixture
def root(tmp_path: Path) -> Path:
    assert tm(tmp_path, "init")[0] == 0
    code, output = tm(tmp_path, "import", stdin=json.dumps(REVIEWED_PLAN))
    assert code == 0, output
    return tmp_path


@pytest.mark.parametrize(("create", "created"), [(by_task_add, 0), (by_api, 201)])
@pytest.mark.parametrize(
    ("fields", "flags"),
    [
        ({}, (False, False, Merge.PARENT)),
        ({"frontmatter": {"sensitive": "migration"}}, (True, True, Merge.PARENT)),
        ({"frontmatter": {"declared_files": [MIGRATION]}}, (True, True, Merge.PARENT)),
    ],
    ids=["plain", "sensitive-key", "writes-a-migration"],
)
def test_a_child_added_under_a_reviewed_plan_takes_its_own_review_only_when_sensitive(
    root: Path,
    create: Create,
    created: int,
    fields: dict[str, Any],
    flags: tuple[bool, bool, Merge],
) -> None:
    code, output = create(root, fields)
    assert code == created, output
    node = child(root)
    assert node is not None and (node.review, node.fix, node.merge) == flags


@pytest.mark.parametrize(("create", "refused"), [(by_import, 1), (by_task_add, 1), (by_api, 400)])
@pytest.mark.parametrize(
    "fields",
    [{"merge": "main"}, {"merge": "main", "review": False, "fix": False}],
    ids=["review-by-default", "review-stated-off"],
)
def test_a_child_landing_on_main_unreviewed_under_a_reviewed_plan_is_refused_and_not_written(
    root: Path, create: Create, refused: int, fields: dict[str, Any]
) -> None:
    code, output = create(root, fields)
    assert code == refused, output
    assert UNREVIEWED_ON_MAIN in output
    assert child(root) is None
