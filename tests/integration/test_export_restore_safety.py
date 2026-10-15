"""Export names files from stored ids safely and replaces the previous export; restore is atomic."""

import json
from pathlib import Path

from typer.testing import CliRunner

from taskmanager.cli.main import app
from taskmanager.core.enums import NodeKind
from taskmanager.core.models import Node
from taskmanager.db.node_repo import NodeRepository
from taskmanager.di.container import create_container
from taskmanager.engine.operations import Operations
from taskmanager.renderers.importers import BulkImporter

runner = CliRunner()


def init(root: Path) -> None:
    root.mkdir()
    assert runner.invoke(app, ["init", "-C", str(root)]).exit_code == 0


def seeded(root: Path) -> None:
    init(root)
    ops = create_container(root).get(Operations)
    ops.add_plan("P", ops.add_spec("S", slug="S1"), slug="P1")


def test_export_writes_a_stored_id_with_path_parts_inside_the_directory(tmp_path: Path) -> None:
    root = tmp_path / "root"
    init(root)
    repo = create_container(root).get(NodeRepository)
    repo.save_node(Node(id="../../escaped", kind=NodeKind.PLAN, title="p"))
    repo.save_node(Node(id="a/b", kind=NodeKind.SPEC, title="s"))
    out = tmp_path / "deep" / "deeper" / "out"

    res = runner.invoke(app, ["export", str(out), "-C", str(root)])

    assert res.exit_code == 0, res.output
    assert sorted(p.name for p in out.iterdir()) == [
        "..%2F..%2Fescaped.json",
        "_format.json",
        "_spec-a%2Fb.json",
    ]
    assert [p.name for p in (tmp_path / "deep").iterdir()] == ["deeper"]
    assert [p.name for p in (tmp_path / "deep" / "deeper").iterdir()] == ["out"]
    plan = json.loads((out / "..%2F..%2Fescaped.json").read_text(encoding="utf-8"))
    assert plan["plans"][0]["id"] == "../../escaped"


def test_export_again_drops_the_files_it_no_longer_emits(tmp_path: Path) -> None:
    root = tmp_path / "root"
    seeded(root)
    create_container(root).get(BulkImporter).import_dict({"tasks": [{"id": "LONE", "title": "l"}]})
    out = tmp_path / "out"
    assert runner.invoke(app, ["export", str(out), "-C", str(root)]).exit_code == 0
    assert (out / "_tasks.json").is_file()
    moved = runner.invoke(app, ["task", "move", "LONE", "--plan", "S1-P1", "-C", str(root)])
    assert moved.exit_code == 0, moved.output

    assert runner.invoke(app, ["export", str(out), "-C", str(root)]).exit_code == 0

    assert not (out / "_tasks.json").exists()
    plan = json.loads((out / "S1-P1.json").read_text(encoding="utf-8"))
    assert "LONE" in [t["id"] for t in plan["plans"][0]["tasks"]]


def test_export_leaves_json_it_did_not_write_in_a_directory_without_the_marker(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    seeded(root)
    out = tmp_path / "out"
    out.mkdir()
    (out / "theirs.json").write_text("{}", encoding="utf-8")

    assert runner.invoke(app, ["export", str(out), "-C", str(root)]).exit_code == 0

    assert (out / "theirs.json").read_text(encoding="utf-8") == "{}"


def test_restore_refused_partway_leaves_the_estate_empty_and_copies_no_assets(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    seeded(source)
    (source / ".taskmanager" / "assets").mkdir(parents=True, exist_ok=True)
    (source / ".taskmanager" / "assets" / "a.txt").write_text("x", encoding="utf-8")
    out = tmp_path / "out"
    assert runner.invoke(app, ["export", str(out), "-C", str(source)]).exit_code == 0
    (out / "_spec-Z.json").write_text(json.dumps({"spec": {"id": "Z"}}), encoding="utf-8")
    fresh = tmp_path / "fresh"
    fresh.mkdir()

    res = runner.invoke(app, ["restore", str(out), "-C", str(fresh)])

    assert res.exit_code == 1, res.output
    assert create_container(fresh).get(NodeRepository).list_nodes() == []
    assert not (fresh / ".taskmanager" / "assets").exists()
