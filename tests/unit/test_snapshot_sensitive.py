from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from taskmanager.core.enums import NodeKind, RelationType
from taskmanager.core.lifecycle import Cycle, next_action
from taskmanager.core.models import Node, NodeRelation
from taskmanager.core.status import Action, Outcome, Status
from taskmanager.db.connection import DatabaseManager
from taskmanager.db.job_repo import JobRepository
from taskmanager.db.node_repo import NodeRepository
from taskmanager.db.runtime_repo import RuntimeRepository
from taskmanager.engine.snapshot import SnapshotBuilder, cycle_in

FIXED: dict[str, Any] = {
    "status": Status.FIXED,
    "review": True,
    "fix": True,
    "outcome": Outcome.REJECT,
    "fix_for": Outcome.REJECT,
    "review_cycles": 1,
}
# (id, kind, parent, fields): four fixed containers, none sensitive by its own frontmatter.
TREE: list[tuple[str, NodeKind, str | None, dict[str, Any]]] = [
    ("MIG", NodeKind.PLAN, None, FIXED),
    (
        "MIG-T",
        NodeKind.TASK,
        "MIG",
        {"frontmatter": {"declared_files": ["api/migrations/versions/0001_t.py"]}},
    ),
    ("RLS", NodeKind.SPEC, None, FIXED),
    ("RLS-P", NodeKind.PLAN, "RLS", {}),
    ("RLS-P-T", NodeKind.TASK, "RLS-P", {"frontmatter": {"sensitive": ["rls"]}}),
    ("PLAIN", NodeKind.PLAN, None, FIXED),
    ("PLAIN-T", NodeKind.TASK, "PLAIN", {"frontmatter": {"declared_files": ["api/app.py"]}}),
    ("SUP", NodeKind.PLAN, None, FIXED),
    (
        "SUP-OLD",
        NodeKind.TASK,
        "SUP",
        {"status": Status.SUPERSEDED, "frontmatter": {"sensitive": "tenant"}},
    ),
    ("SUP-NEW", NodeKind.TASK, "SUP", {"status": Status.COMPLETED}),
]

Reader = Callable[[SnapshotBuilder, Node], Cycle]


def live(builder: SnapshotBuilder, node: Node) -> Cycle:
    return builder.cycle(node)


def from_snapshot(builder: SnapshotBuilder, node: Node) -> Cycle:
    return cycle_in(builder.build(), node)


@pytest.fixture
def builder(tmp_path: Path) -> SnapshotBuilder:
    db = DatabaseManager(tmp_path / ".taskmanager")
    db.init_all()
    nodes = NodeRepository(db)
    for node_id, kind, parent, fields in TREE:
        nodes.save_node(
            Node.model_validate({"id": node_id, "kind": kind, "title": node_id, **fields})
        )
        if parent is not None:
            nodes.add_relation(
                NodeRelation(
                    source_id=parent, target_id=node_id, relation_type=RelationType.CONTAINS
                )
            )
    return SnapshotBuilder(nodes, RuntimeRepository(db), JobRepository(db))


@pytest.mark.parametrize("read", [live, from_snapshot], ids=["live", "snapshot"])
@pytest.mark.parametrize(
    ("container", "sensitive"),
    [("MIG", True), ("RLS", True), ("PLAIN", False), ("SUP", True)],
    ids=[
        "migration-child",
        "rls-grandchild",
        "neither",
        "superseded-sensitive-child",
    ],
)
def test_a_fixed_container_is_re_reviewed_only_when_a_node_under_it_is_sensitive(
    builder: SnapshotBuilder, read: Reader, container: str, sensitive: bool
) -> None:
    node = builder.node_repo.get_node(container)
    assert node is not None

    cycle = read(builder, node)

    expected = Action.REVIEW if sensitive else Action.MERGE
    assert (cycle.sensitive, next_action(cycle)) == (sensitive, expected)
