"""Chains on different targets: each meets within its own target, and a link across two is
refused where it is written."""

import json
from dataclasses import replace
from itertools import product
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from lifecycle_estate import add, make_estate, stored
from typer.testing import CliRunner

from taskmanager.cli.main import app
from taskmanager.core.enums import NodeKind
from taskmanager.core.status import Merge, Status
from taskmanager.db.connection import DatabaseManager
from taskmanager.db.node_repo import NodeRepository
from taskmanager.engine.chains import (
    base_chain,
    landing_chain,
    landing_target,
    meeting,
    satisfied,
    sync_pairs,
    target,
)
from taskmanager.engine.claims import Claims
from taskmanager.engine.discovery import discover
from taskmanager.engine.stepgraph import SnapNode, Snapshot, find_cycle, migration_holders
from taskmanager.web.app import create_app

SPEC, PLAN, TASK = NodeKind.SPEC, NodeKind.PLAN, NodeKind.TASK
PARENT, ON_SPEC = Merge.PARENT, Merge.SPEC
MIGRATION = ["api/migrations/versions/001_add.py"]


def three_targets() -> Snapshot:
    """R lands on release/1, D on develop and M on main, each a spec over a plan whose tasks land
    on the plan (a) or straight on the spec's target (b); a sub-plan under R's plan nests one
    level deeper."""
    nodes: list[SnapNode] = []
    for spec, top in (("R", "release/1"), ("D", "develop"), ("M", "main")):
        nodes += [
            SnapNode(spec, SPEC, top=top, land_on=top),
            SnapNode(f"{spec}P", PLAN, parent=spec, merge=PARENT, top=top),
            SnapNode(f"{spec}a", TASK, parent=f"{spec}P", merge=PARENT, top=top),
            SnapNode(f"{spec}b", TASK, parent=f"{spec}P", merge=ON_SPEC, top=top),
        ]
    nodes += [
        SnapNode("RQ", PLAN, parent="RP", merge=PARENT, top="release/1"),
        SnapNode("RQa", TASK, parent="RQ", merge=PARENT, top="release/1"),
    ]
    return Snapshot({n.id: n for n in nodes}, [])


def test_each_chain_ends_at_its_own_target() -> None:
    s = three_targets()

    assert base_chain(s, "RQa") == ["RQ", "RP", "R", "TOP:release/1"]
    assert base_chain(s, "Db") == ["TOP:develop"]
    assert landing_chain(s, "Ma") == ["Ma", "MP", "M"]
    assert {n: target(s, n) for n in ("RQa", "Rb", "DP", "Ma")} == {
        "RQa": "release/1",
        "Rb": "release/1",
        "DP": "develop",
        "Ma": "main",
    }


def test_meeting_never_raises_and_meets_within_one_target_for_every_pair() -> None:
    s = three_targets()

    for x, y in product(s.nodes, repeat=2):
        z = meeting(s, x, y)
        assert z in landing_chain(s, y)
        if target(s, x) == target(s, y):
            assert landing_target(s, z) in base_chain(s, x)
        else:
            assert z == landing_chain(s, y)[-1]
            assert sync_pairs(s, x, y) == []
            assert satisfied(s, x, y) is False


def test_a_dependency_on_another_target_is_satisfied_once_it_lands_on_its_own() -> None:
    s = three_targets()
    landed = Snapshot(
        {**s.nodes, "M": SnapNode("M", SPEC, top="main", status=Status.COMPLETED)}, []
    )

    assert satisfied(s, "Ra", "Ma") is False
    assert satisfied(landed, "Ra", "Ma") is True


def test_the_step_graph_holds_a_cross_target_edge_and_migration_chain_without_raising() -> None:
    s = three_targets()
    writers = {n: replace(s.nodes[n], repo="api", writes_migration=True) for n in ("Ra", "Ma")}
    crossed = Snapshot({**s.nodes, **writers}, [("Db", "RQa")])

    assert find_cycle(crossed) is None
    assert len(migration_holders(crossed, "api")) == 1


def batch(claims: Claims) -> dict[str, Any]:
    payload, _ = discover(claims, specs=None, session="s1", slots=10, max_strong=10)
    data: dict[str, Any] = json.loads(payload)
    return data


def two_specs(tmp_path: Path) -> Claims:
    """A lands on release/x, B on main; each has a plan whose second task waits on its first."""
    claims = make_estate(tmp_path)
    for spec in ("A", "B"):
        add(claims, spec, SPEC)
        add(claims, f"{spec}P", PLAN, parent=spec)
        add(claims, f"{spec}1", parent=f"{spec}P", merge=PARENT)
        add(claims, f"{spec}2", parent=f"{spec}P", merge=PARENT, depends=(f"{spec}1",))
    claims.ops.update_node("A", frontmatter_set={"land_on": "release/x"})
    return claims


def test_two_specs_on_different_targets_with_no_edges_discover_and_order_independently(
    tmp_path: Path,
) -> None:
    claims = two_specs(tmp_path)
    snap = claims.snapshots.build()
    assert (base_chain(snap, "A2"), base_chain(snap, "B2")) == (
        ["AP", "TOP:release/x"],
        ["BP", "TOP:main"],
    )

    data = batch(claims)
    assert [entry["id"] for entry in data["chosen"]] == ["A1", "B1"]
    assert {"A2: waits on A1", "B2: waits on B1"} <= set(data["held"])

    claims.nodes.save_node(stored(claims, "A1").model_copy(update={"status": Status.COMPLETED}))
    data = batch(claims)
    assert [entry["id"] for entry in data["chosen"]] == ["A2", "B1"]
    assert "B2: waits on B1" in data["held"]


def test_a_cross_target_edge_already_stored_holds_its_dependent_and_blocks_no_other_write(
    tmp_path: Path,
) -> None:
    claims = two_specs(tmp_path)
    add(claims, "B3", parent="BP", merge=PARENT, depends=("A1",))

    assert "B3: waits on A1" in batch(claims)["held"]
    claims.ops.update_node("B2", priority=10)
    assert stored(claims, "B2").priority == 10

    for node_id in ("A1", "AP"):
        claims.nodes.save_node(
            stored(claims, node_id).model_copy(update={"status": Status.COMPLETED})
        )
    assert "B3" in [entry["id"] for entry in batch(claims)["chosen"]]


def tm(root: Path, *args: str, stdin: str | None = None) -> tuple[int, str]:
    result = CliRunner().invoke(app, [*args, "-C", str(root)], input=stdin)
    return result.exit_code, " ".join(result.output.split())


def doc(spec: str, land_on: str | None = None, **task: Any) -> str:
    head: dict[str, Any] = {"id": spec, "title": spec}
    if land_on is not None:
        head["frontmatter"] = {"land_on": land_on}
    plan = {"id": f"{spec}-P", "title": "P", "tasks": [{"id": f"{spec}-P-a", "title": "a", **task}]}
    return json.dumps({"spec": head, "plans": [plan]})


def dependencies(root: Path, node_id: str) -> list[str]:
    return NodeRepository(DatabaseManager(root / ".taskmanager")).get_dependencies(node_id)


@pytest.fixture
def root(tmp_path: Path) -> Path:
    assert tm(tmp_path, "init")[0] == 0
    for spec, land_on in (("A", "release/x"), ("B", None)):
        code, output = tm(tmp_path, "import", stdin=doc(spec, land_on))
        assert code == 0, output
    return tmp_path


CROSSED = (
    "B-P-a: depends on A-P-a, which lands on release/x, but B-P-a lands on main; remove the "
    "edge, or land both on one target"
)


def test_import_refuses_an_edge_to_a_node_on_another_target_and_writes_nothing(
    root: Path,
) -> None:
    code, output = tm(root, "import", stdin=doc("C", depends_on=["A-P-a"]))

    assert code == 1
    assert "C-P-a: depends on A-P-a, which lands on release/x, but C-P-a lands on main" in output
    assert NodeRepository(DatabaseManager(root / ".taskmanager")).get_node("C") is None


def test_import_takes_an_edge_to_a_node_on_the_same_target(root: Path) -> None:
    code, output = tm(root, "import", stdin=doc("C", "release/x", depends_on=["A-P-a"]))

    assert code == 0, output
    assert dependencies(root, "C-P-a") == ["A-P-a"]


def test_task_depends_refuses_an_edge_to_a_node_on_another_target(root: Path) -> None:
    code, output = tm(root, "task", "depends", "B-P-a", "--add", "A-P-a")

    assert code == 1
    assert CROSSED in output
    assert dependencies(root, "B-P-a") == []


def test_web_dependency_route_refuses_an_edge_to_a_node_on_another_target(root: Path) -> None:
    client = TestClient(create_app(root))

    response = client.post("/api/nodes/B-P-a/dependencies", json={"add": [{"id": "A-P-a"}]})

    assert response.status_code == 409
    assert CROSSED in response.json()["detail"]
    assert dependencies(root, "B-P-a") == []


def test_an_edge_on_a_container_whose_work_lands_on_another_target_is_refused(
    root: Path,
) -> None:
    code, output = tm(root, "task", "depends", "B-P", "--add", "A-P-a")

    assert code == 1
    assert "B-P: depends on A-P-a, which lands on release/x, but B-P lands on main" in output


def test_moving_a_spec_s_land_on_across_an_edge_is_refused(root: Path) -> None:
    assert tm(root, "task", "update", "B", "--set", "land_on=release/x")[0] == 0
    assert tm(root, "task", "depends", "B-P-a", "--add", "A-P-a")[0] == 0

    code, output = tm(root, "task", "update", "B", "--set", "land_on=release/y")

    assert code == 1
    assert "B-P-a: depends on A-P-a, which lands on release/x, but B-P-a lands on release/y" in (
        output
    )


def test_import_refuses_a_migration_chain_across_targets(root: Path) -> None:
    writer = {"target_repo": "api", "frontmatter": {"declared_files": MIGRATION}}
    assert tm(root, "import", stdin=doc("C", "release/x", **writer))[0] == 0

    code, output = tm(root, "import", stdin=doc("D", **writer))

    assert code == 1
    assert (
        "C-P-a: writes api's migrations on release/x while D-P-a writes them on main; land one "
        "of them first, or land both on one target"
    ) in output
