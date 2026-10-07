"""Export writes the lifecycle format with its marker; restore reads only that format."""

import json
from pathlib import Path

from typer.testing import CliRunner

from taskmanager.cli.main import app
from taskmanager.core.status import ConditionStage, Merge
from taskmanager.di.container import create_container
from taskmanager.engine.operations import Operations
from taskmanager.renderers.importers import BulkImporter

runner = CliRunner()
CHECK = "curl -fsS https://staging.example/health"


def seeded(root: Path) -> None:
    assert runner.invoke(app, ["init", "-C", str(root)]).exit_code == 0
    ops = create_container(root).get(Operations)
    spec = ops.add_spec("S", slug="S1")
    plan = ops.add_plan("P", spec, slug="P1", review=True, fix=True)
    a = ops.add_task("a", plan, slug="a", merge=Merge.PARENT, requires=["figma"])
    # b depends on a, which only reaches MAIN once the plan lands; b must land through the
    # plan too, or that dependency and b's own containment close a cycle.
    ops.add_task("b", plan, slug="b", merge=Merge.PARENT, depends_on=[a])
    ops.update_node(a, review=True)
    ops.update_node(plan, land_order=["api", "web"])
    ops.add_condition(a, "staging up", CHECK, ConditionStage.LANDING)


def test_export_writes_the_format_marker_flags_conditions_and_bare_dependencies(
    tmp_path: Path,
) -> None:
    seeded(tmp_path)
    out = tmp_path / "e"
    assert runner.invoke(app, ["export", str(out), "-C", str(tmp_path)]).exit_code == 0
    assert json.loads((out / "_format.json").read_text()) == {
        "format": "tm-lifecycle",
        "version": 1,
    }
    plan = json.loads((out / "S1-P1.json").read_text())["plans"][0]
    a, b = plan["tasks"]
    assert (plan["status"], plan["review"], plan["fix"], plan["land_order"]) == (
        "READY",
        True,
        True,
        ["api", "web"],
    )
    assert (a["status"], a["review"], a["fix"], a["merge"], a["requires"]) == (
        "READY",
        True,
        False,
        "parent",
        ["figma"],
    )
    assert a["conditions"] == [{"needs": "staging up", "command": CHECK, "stage": "landing"}]
    assert b["depends_on"] == ["S1-P1-a"]


def test_restore_rebuilds_an_export_that_exports_byte_identically(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    seeded(source)
    assert runner.invoke(app, ["export", str(tmp_path / "e1"), "-C", str(source)]).exit_code == 0
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    res = runner.invoke(app, ["restore", str(tmp_path / "e1"), "-C", str(fresh)])
    assert res.exit_code == 0, res.output
    assert runner.invoke(app, ["export", str(tmp_path / "e2"), "-C", str(fresh)]).exit_code == 0
    for f in sorted((tmp_path / "e1").glob("*.json")):
        assert f.read_bytes() == (tmp_path / "e2" / f.name).read_bytes(), f.name


def test_restore_refuses_a_pre_lifecycle_export_and_writes_nothing(tmp_path: Path) -> None:
    old = tmp_path / "old"
    old.mkdir()
    (old / "S1-P1.json").write_text(
        json.dumps(
            {"spec": None, "plans": [{"id": "S1-P1", "title": "P", "status": "NOT_STARTED"}]}
        ),
        encoding="utf-8",
    )
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    res = runner.invoke(app, ["restore", str(old), "-C", str(fresh)])
    assert res.exit_code == 1
    assert "v0.2.0" in res.output and "tm import" in res.output
    assert not (fresh / ".taskmanager").exists()


def test_tasks_a_spec_or_nothing_holds_export_and_restore_byte_identically(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    seeded(source)
    importer = create_container(source).get(BulkImporter)
    importer.import_dict(
        {"spec": {"id": "S2", "title": "T"}, "tasks": [{"id": "ST", "title": "t"}]}
    )
    importer.import_dict({"tasks": [{"id": "LONE", "title": "l", "depends_on": ["S1-P1-a"]}]})
    ops = create_container(source).get(Operations)
    ops.set_dependencies("S1-P1-b", ["ST"], [])

    e1, e2 = tmp_path / "e1", tmp_path / "e2"
    assert runner.invoke(app, ["export", str(e1), "-C", str(source)]).exit_code == 0
    spec = json.loads((e1 / "_spec-S2.json").read_text())
    assert [t["id"] for t in spec["tasks"]] == ["ST"]
    assert [t["id"] for t in json.loads((e1 / "_tasks.json").read_text())["tasks"]] == ["LONE"]

    fresh = tmp_path / "fresh"
    fresh.mkdir()
    res = runner.invoke(app, ["restore", str(e1), "-C", str(fresh)])
    assert res.exit_code == 0, res.output
    assert runner.invoke(app, ["export", str(e2), "-C", str(fresh)]).exit_code == 0
    assert sorted(f.name for f in e1.glob("*.json")) == sorted(f.name for f in e2.glob("*.json"))
    for f in sorted(e1.glob("*.json")):
        assert f.read_bytes() == (e2 / f.name).read_bytes(), f.name
