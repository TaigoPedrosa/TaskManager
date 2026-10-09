import json
from pathlib import Path

from typer.testing import CliRunner

from taskmanager.cli.main import EXPORT_FORMAT, app
from taskmanager.db.connection import DatabaseManager
from taskmanager.db.node_repo import NodeRepository

runner = CliRunner()


def _estate(tmp_path: Path) -> Path:
    root = tmp_path / "estate"
    root.mkdir()
    assert runner.invoke(app, ["init", "-C", str(root)]).exit_code == 0
    return root


def _import(root: Path, document: dict[str, object]) -> tuple[int, str]:
    path = root.parent / "doc.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    result = runner.invoke(app, ["import", "-f", str(path), "-C", str(root)])
    return result.exit_code, result.stderr


def _repo(root: Path) -> NodeRepository:
    return NodeRepository(DatabaseManager(root / ".taskmanager"))


PLAN = {"id": "S-P", "title": "P", "tasks": [{"id": "S-P-a", "title": "a"}]}


def test_an_import_with_plans_and_no_spec_is_refused(tmp_path: Path) -> None:
    root = _estate(tmp_path)

    code, stderr = _import(root, {"plans": [PLAN]})

    assert code != 0
    assert "spec: {id: <spec-id>}" in stderr
    assert _repo(root).get_node("S-P") is None
    assert _repo(root).get_node("S-P-a") is None


def test_an_import_naming_an_existing_spec_by_id_adds_its_plans_and_keeps_the_spec(
    tmp_path: Path,
) -> None:
    root = _estate(tmp_path)
    spec = {"id": "S", "title": "Spec", "sections": {"context": "why"}}
    assert _import(root, {"spec": spec}) == (0, "")

    code, stderr = _import(root, {"spec": {"id": "S"}, "plans": [PLAN]})

    assert code == 0, stderr
    repo = _repo(root)
    kept = repo.get_node("S")
    section = repo.get_section("S", "context")
    assert kept is not None and section is not None
    assert (kept.title, section.content) == ("Spec", "why")
    assert repo.get_children("S") == ["S-P"]


def test_top_level_tasks_under_a_spec_import_as_before(tmp_path: Path) -> None:
    root = _estate(tmp_path)

    code, stderr = _import(
        root, {"spec": {"id": "S", "title": "S"}, "tasks": [{"id": "S-a", "title": "a"}]}
    )

    assert code == 0, stderr
    assert _repo(root).get_children("S") == ["S-a"]


def test_reimporting_a_placed_plan_without_its_spec_keeps_it_under_the_spec(
    tmp_path: Path,
) -> None:
    root = _estate(tmp_path)
    assert _import(root, {"spec": {"id": "S", "title": "S"}, "plans": [PLAN]}) == (0, "")

    code, stderr = _import(root, {"plans": [{**PLAN, "title": "P renamed"}]})

    assert code == 0, stderr
    plan = _repo(root).get_node("S-P")
    assert plan is not None and plan.title == "P renamed"
    assert _repo(root).get_parent_ids("S-P") == ["S"]


def test_a_restore_takes_back_a_plan_its_export_holds_under_no_spec(tmp_path: Path) -> None:
    export = tmp_path / "export"
    export.mkdir()
    (export / "_format.json").write_text(json.dumps(EXPORT_FORMAT), encoding="utf-8")
    (export / "S-P.json").write_text(json.dumps({"spec": None, "plans": [PLAN]}), encoding="utf-8")
    root = tmp_path / "restored"

    result = runner.invoke(app, ["restore", str(export), "-C", str(root)])

    assert result.exit_code == 0, result.stderr
    assert _repo(root).get_node("S-P") is not None
    assert _repo(root).get_parent_ids("S-P") == []
