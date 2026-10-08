"""A spec's `land_on` is set and shown through the CLI and the web, each route refused at the
write's own check: a name git rejects, and `land_on` anywhere but on a spec."""

from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from taskmanager.cli.main import app
from taskmanager.web.app import create_app

runner = CliRunner()
WIDE = {"COLUMNS": "200"}


def tm(root: Path, *args: str) -> tuple[int, str]:
    res = runner.invoke(app, [*args, "--path", str(root)], env=WIDE)
    return res.exit_code, res.output


@pytest.fixture
def estate(tmp_path: Path) -> Path:
    tm(tmp_path, "init")
    return tmp_path


def chain(root: Path, spec_args: list[str]) -> None:
    """S > S-P > S-P-T, the task landing on its plan's branch."""
    assert tm(root, "spec", "add", "Spec", "--slug", "S", *spec_args)[0] == 0
    assert tm(root, "plan", "add", "Plan", "--spec", "S", "--slug", "P")[0] == 0
    assert (
        tm(root, "task", "add", "Task", "--plan", "S-P", "--slug", "T", "--merge", "parent")[0] == 0
    )


def task_get(root: Path, node_id: str) -> dict[str, object]:
    code, out = tm(root, "task", "get", node_id, "--yaml")
    assert code == 0, out
    return yaml.safe_load(out)


def test_spec_add_land_on_is_the_branch_task_get_names_for_every_node_under_it(
    estate: Path,
) -> None:
    chain(estate, ["--land-on", "release/0.4"])
    assert task_get(estate, "S")["frontmatter"] == {"land_on": "release/0.4"}
    for node_id in ("S", "S-P", "S-P-T"):
        assert task_get(estate, node_id)["lands_on"] == "release/0.4"
    assert task_get(estate, "S-P-T")["landing_chain"] == ["S-P-T", "S-P"]


def test_without_land_on_task_get_names_the_default_branch(estate: Path) -> None:
    chain(estate, [])
    assert task_get(estate, "S-P-T")["lands_on"] == "main"


def test_spec_add_refuses_a_branch_name_git_rejects_and_writes_nothing(estate: Path) -> None:
    code, out = tm(estate, "spec", "add", "Spec", "--slug", "S", "--land-on", "release..0.4")
    assert code == 1
    assert "land_on 'release..0.4' is not a branch name git accepts" in out
    assert tm(estate, "task", "get", "S")[0] == 1


def test_merge_help_names_both_targets(estate: Path) -> None:
    for command in (["plan", "add"], ["task", "add"], ["task", "update"]):
        code, out = tm(estate, *command, "--help")
        assert code == 0
        assert "parent|spec" in out
        assert "Land on the parent's branch, or where the spec lands" in out


@pytest.fixture
def web(estate: Path) -> TestClient:
    chain(estate, [])
    return TestClient(create_app(estate))


def detail(client: TestClient, node_id: str) -> dict[str, object]:
    res = client.get(f"/api/nodes/{node_id}")
    assert res.status_code == 200, res.text
    node: dict[str, object] = res.json()["node"]
    return node


def test_post_spec_writes_land_on_and_its_nodes_name_the_branch(web: TestClient) -> None:
    res = web.post("/api/specs", json={"title": "R", "slug": "R", "land_on": "release/0.4"})
    assert res.status_code == 201, res.text
    assert web.post("/api/plans", json={"title": "Q", "spec": "R", "slug": "Q"}).status_code == 201
    assert detail(web, "R")["frontmatter"] == {"land_on": "release/0.4"}
    assert detail(web, "R-Q")["base_chain"] == ["release/0.4"]


def test_post_spec_refuses_a_branch_name_git_rejects(web: TestClient) -> None:
    res = web.post("/api/specs", json={"title": "R", "slug": "R", "land_on": "release..0.4"})
    assert res.status_code == 400
    assert "land_on 'release..0.4' is not a branch name git accepts" in res.json()["detail"]
    assert web.get("/api/nodes/R").status_code == 404


def test_post_plan_refuses_land_on(web: TestClient) -> None:
    res = web.post("/api/plans", json={"title": "Q", "spec": "S", "slug": "Q", "land_on": "x"})
    assert res.status_code == 400
    assert "land_on is set on a spec only" in res.json()["detail"]
    assert web.get("/api/nodes/S-Q").status_code == 404


def test_patch_spec_land_on_moves_every_lands_line_under_it(web: TestClient) -> None:
    assert detail(web, "S-P-T")["base_chain"] == ["S-P", "main"]
    res = web.patch("/api/nodes/S", json={"land_on": "release/0.4"})
    assert res.status_code == 200, res.text
    assert res.json() == {"frontmatter.land_on": "release/0.4"}
    assert detail(web, "S")["base_chain"] == ["release/0.4"]
    assert detail(web, "S-P-T")["base_chain"] == ["S-P", "release/0.4"]
    unset = web.patch("/api/nodes/S", json={"frontmatter_unset": ["land_on"]})
    assert unset.status_code == 200, unset.text
    assert detail(web, "S-P-T")["base_chain"] == ["S-P", "main"]


def test_patch_spec_refuses_a_branch_name_git_rejects(web: TestClient) -> None:
    res = web.patch("/api/nodes/S", json={"land_on": "release..0.4"})
    assert res.status_code == 400
    assert "land_on 'release..0.4' is not a branch name git accepts" in res.json()["detail"]
    assert detail(web, "S")["frontmatter"] == {}


def test_patch_refuses_land_on_on_a_node_that_lands_on_its_parent(web: TestClient) -> None:
    assert detail(web, "S-P-T")["merge"] == "parent"
    res = web.patch("/api/nodes/S-P-T", json={"land_on": "release/0.4"})
    assert res.status_code == 400
    assert "land_on is set on a spec only" in res.json()["detail"]
    assert detail(web, "S-P-T")["frontmatter"] == {}


def test_meta_offers_parent_and_spec_as_merge_targets(web: TestClient) -> None:
    assert web.get("/api/meta").json()["merge_targets"] == ["parent", "spec"]
