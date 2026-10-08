import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from taskmanager.cli.main import app
from taskmanager.core.models import Node
from taskmanager.db.connection import DatabaseManager
from taskmanager.db.node_repo import NodeRepository
from taskmanager.web.app import create_app

REFUSAL = "merge is parent or spec; main is now spec"
SPEC = {"id": "S", "title": "S"}


def tm(root: Path, *args: str, stdin: str | None = None) -> tuple[int, str]:
    result = CliRunner().invoke(app, [*args, "-C", str(root)], input=stdin)
    # Rich wraps a long line at the terminal width.
    return result.exit_code, " ".join(result.output.split())


def stored(root: Path, node_id: str) -> Node | None:
    return NodeRepository(DatabaseManager(root / ".taskmanager")).get_node(node_id)


@pytest.fixture
def root(tmp_path: Path) -> Path:
    assert tm(tmp_path, "init")[0] == 0
    plan = {"id": "S-P", "title": "P", "tasks": [{"id": "S-P-a", "title": "a"}]}
    code, output = tm(tmp_path, "import", stdin=json.dumps({"spec": SPEC, "plans": [plan]}))
    assert code == 0, output
    return tmp_path


@pytest.fixture
def client(root: Path) -> TestClient:
    return TestClient(create_app(root))


def test_import_merge_main_is_refused_and_writes_nothing(root: Path) -> None:
    plan = {"id": "S-P", "title": "P", "tasks": [{"id": "S-P-b", "title": "b", "merge": "main"}]}

    code, output = tm(root, "import", stdin=json.dumps({"spec": SPEC, "plans": [plan]}))

    assert (code, output) == (1, f"import refused, nothing written: stdin: node 'S-P-b': {REFUSAL}")
    assert stored(root, "S-P-b") is None


def test_plan_add_merge_main_is_refused_and_writes_nothing(root: Path) -> None:
    code, output = tm(root, "plan", "add", "Q", "--spec", "S", "--slug", "Q", "--merge", "main")

    assert (code, output) == (1, REFUSAL)
    assert stored(root, "S-Q") is None


def test_task_add_merge_main_is_refused_and_writes_nothing(root: Path) -> None:
    code, output = tm(root, "task", "add", "b", "--plan", "S-P", "--slug", "b", "--merge", "main")

    assert (code, output) == (1, REFUSAL)
    assert stored(root, "S-P-b") is None


def test_task_update_merge_main_is_refused_and_writes_nothing(root: Path) -> None:
    before = stored(root, "S-P-a")

    code, output = tm(root, "task", "update", "S-P-a", "--merge", "main")

    assert (code, output) == (1, REFUSAL)
    assert stored(root, "S-P-a") == before


def test_web_plan_create_merge_main_is_refused_and_writes_nothing(
    root: Path, client: TestClient
) -> None:
    response = client.post(
        "/api/plans", json={"title": "Q", "spec": "S", "slug": "Q", "merge": "main"}
    )

    assert (response.status_code, response.json()) == (400, {"detail": REFUSAL})
    assert stored(root, "S-Q") is None


def test_web_task_create_merge_main_is_refused_and_writes_nothing(
    root: Path, client: TestClient
) -> None:
    response = client.post(
        "/api/tasks", json={"title": "b", "plan": "S-P", "slug": "b", "merge": "main"}
    )

    assert (response.status_code, response.json()) == (400, {"detail": REFUSAL})
    assert stored(root, "S-P-b") is None


def test_web_update_merge_main_is_refused_and_writes_nothing(
    root: Path, client: TestClient
) -> None:
    before = stored(root, "S-P-a")

    response = client.patch("/api/nodes/S-P-a", json={"merge": "main"})

    assert (response.status_code, response.json()) == (400, {"detail": REFUSAL})
    assert stored(root, "S-P-a") == before


def test_merge_option_names_the_enum_values() -> None:
    result = CliRunner().invoke(app, ["task", "update", "--help"])

    assert "parent|spec" in result.output
