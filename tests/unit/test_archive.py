from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from taskmanager.core.enums import NodeKind, RelationType
from taskmanager.core.models import LedgerEvent, Node, NodeRelation
from taskmanager.core.status import Status
from taskmanager.db.connection import DatabaseManager
from taskmanager.db.job_repo import JobRepository
from taskmanager.db.ledger_repo import LedgerRepository
from taskmanager.db.node_repo import NodeRepository
from taskmanager.db.runtime_repo import RuntimeRepository
from taskmanager.engine.archive import Archive, archived_specs
from taskmanager.engine.snapshot import SnapshotBuilder

NOW = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)


class Estate:
    def __init__(self, root: Path) -> None:
        db = DatabaseManager(root / ".taskmanager")
        db.init_all()
        self.nodes = NodeRepository(db)
        self.ledger = LedgerRepository(db)
        self.builder = SnapshotBuilder(self.nodes, RuntimeRepository(db), JobRepository(db))

    def add(
        self, node_id: str, kind: NodeKind, parent: str | None = None, status: Status = Status.READY
    ) -> None:
        self.nodes.save_node(Node(id=node_id, kind=kind, title=node_id, status=status))
        if parent is not None:
            self.nodes.add_relation(
                NodeRelation(
                    source_id=parent, target_id=node_id, relation_type=RelationType.CONTAINS
                )
            )

    def spec(self, spec_id: str, status: Status = Status.COMPLETED) -> None:
        self.add(spec_id, NodeKind.SPEC, status=status)
        self.add(f"{spec_id}-plan", NodeKind.PLAN, parent=spec_id, status=status)
        self.add(f"{spec_id}-task", NodeKind.TASK, parent=f"{spec_id}-plan", status=status)

    def moved(self, node_id: str, to: Status, days_ago: float) -> None:
        self.ledger.append(
            LedgerEvent(
                timestamp=NOW - timedelta(days=days_ago),
                actor_id="tm",
                command="rollup",
                target_id=node_id,
                payload={"from": "READY", "to": to.value},
            )
        )

    def updated(self, node_id: str, days_ago: float) -> None:
        node = self.nodes.get_node(node_id)
        assert node is not None
        self.nodes.save_node(node.model_copy(update={"updated_at": NOW - timedelta(days=days_ago)}))

    def archive(self, days: int = 3) -> Archive:
        return archived_specs(self.builder.build(), self.ledger, NOW, days)


@pytest.fixture
def estate(tmp_path: Path) -> Estate:
    return Estate(tmp_path)


def test_archived_specs_completed_past_the_window_archives_and_within_it_does_not(
    estate: Estate,
) -> None:
    estate.spec("OLD")
    estate.moved("OLD", Status.COMPLETED, days_ago=4)
    estate.spec("NEW")
    estate.moved("NEW", Status.COMPLETED, days_ago=2)

    archive = estate.archive()

    assert archive.specs == {"OLD"}
    assert archive.next_boundary == NOW + timedelta(days=1)


def test_archived_specs_later_event_on_a_completed_spec_keeps_its_completion_time(
    estate: Estate,
) -> None:
    estate.spec("S")
    estate.moved("S", Status.COMPLETED, days_ago=4)
    estate.ledger.append(
        LedgerEvent(
            timestamp=NOW - timedelta(days=1),
            actor_id="tm",
            command="task update",
            target_id="S",
            payload={"title": "S"},
        )
    )

    assert estate.archive().specs == {"S"}


def test_archived_specs_with_zero_days_archives_nothing(estate: Estate) -> None:
    estate.spec("OLD")
    estate.moved("OLD", Status.COMPLETED, days_ago=40)

    assert estate.archive(days=0) == Archive(frozenset(), frozenset(), None)


def test_archived_specs_reopened_spec_leaves_the_archive(estate: Estate) -> None:
    estate.spec("S", status=Status.READY)
    estate.moved("S", Status.COMPLETED, days_ago=5)
    estate.moved("S", Status.READY, days_ago=1)

    archive = estate.archive()

    assert archive.specs == frozenset()
    assert archive.next_boundary is None


def test_archived_specs_completed_again_counts_from_the_last_completion(estate: Estate) -> None:
    estate.spec("S")
    estate.moved("S", Status.COMPLETED, days_ago=5)
    estate.moved("S", Status.READY, days_ago=4)
    estate.moved("S", Status.COMPLETED, days_ago=1)

    archive = estate.archive()

    assert archive.specs == frozenset()
    assert archive.next_boundary == NOW + timedelta(days=2)


def test_archived_specs_completed_spec_with_no_ledger_completion_counts_from_its_last_update(
    estate: Estate,
) -> None:
    estate.spec("OLD")
    estate.updated("OLD", days_ago=4)
    estate.spec("NEW")
    estate.updated("NEW", days_ago=1)

    archive = estate.archive()

    assert archive.specs == {"OLD"}
    assert archive.next_boundary == NOW + timedelta(days=2)


def test_archived_specs_ledger_completion_wins_over_a_later_update(estate: Estate) -> None:
    estate.spec("S")
    estate.moved("S", Status.COMPLETED, days_ago=4)
    estate.updated("S", days_ago=1)

    assert estate.archive().specs == {"S"}


def test_archived_specs_subtree_goes_with_its_spec(estate: Estate) -> None:
    estate.spec("OLD")
    estate.moved("OLD", Status.COMPLETED, days_ago=4)
    estate.spec("LIVE", status=Status.READY)

    assert estate.archive().nodes == {"OLD", "OLD-plan", "OLD-task"}
