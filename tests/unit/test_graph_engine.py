from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from taskmanager.core.enums import (
    NodeKind,
    NodeStatus,
    RelationType,
    VerificationType,
    VirtualStatus,
)
from taskmanager.core.models import FileLock, Lease, Node, NodeRelation, NodeVerification
from taskmanager.db.connection import DatabaseManager
from taskmanager.db.node_repo import NodeRepository
from taskmanager.db.runtime_repo import RuntimeRepository
from taskmanager.engine.graph import GraphEngine


@pytest.fixture
def repos(tmp_path: Path) -> tuple[NodeRepository, RuntimeRepository, GraphEngine]:
    db = DatabaseManager(tmp_path)
    db.init_all()
    node_repo = NodeRepository(db)
    runtime_repo = RuntimeRepository(db)
    engine = GraphEngine(node_repo=node_repo, runtime_repo=runtime_repo)
    return node_repo, runtime_repo, engine


def test_resolve_task_state_ready_vs_blocked(
    repos: tuple[NodeRepository, RuntimeRepository, GraphEngine],
) -> None:
    node_repo, _, engine = repos

    t1 = Node(id="AUTH-T01", kind=NodeKind.TASK, title="Task 1", status=NodeStatus.NOT_STARTED)
    t2 = Node(id="AUTH-T02", kind=NodeKind.TASK, title="Task 2", status=NodeStatus.NOT_STARTED)
    t3 = Node(id="AUTH-T03", kind=NodeKind.TASK, title="Task 3", status=NodeStatus.NOT_STARTED)
    node_repo.save_node(t1)
    node_repo.save_node(t2)
    node_repo.save_node(t3)

    assert engine.resolve_task_state("AUTH-T01") == VirtualStatus.READY

    node_repo.add_relation(
        NodeRelation(
            source_id="AUTH-T02",
            target_id="AUTH-T01",
            relation_type=RelationType.DEPENDS_ON,
        )
    )
    assert engine.resolve_task_state("AUTH-T02") == VirtualStatus.BLOCKED

    t1.status = NodeStatus.IMPLEMENTING
    node_repo.save_node(t1)
    assert engine.resolve_task_state("AUTH-T02") == VirtualStatus.BLOCKED

    t1.status = NodeStatus.COMPLETED
    node_repo.save_node(t1)
    assert engine.resolve_task_state("AUTH-T02") == VirtualStatus.READY

    node_repo.add_relation(
        NodeRelation(
            source_id="AUTH-T03",
            target_id="AUTH-T01",
            relation_type=RelationType.DEPENDS_ON,
        )
    )
    node_repo.add_relation(
        NodeRelation(
            source_id="AUTH-T03",
            target_id="AUTH-T02",
            relation_type=RelationType.DEPENDS_ON,
        )
    )
    assert engine.resolve_task_state("AUTH-T03") == VirtualStatus.BLOCKED

    t2.status = NodeStatus.SUPERSEDED
    node_repo.save_node(t2)
    assert engine.resolve_task_state("AUTH-T03") == VirtualStatus.READY

    t3.status = NodeStatus.WAITING_REVIEW
    node_repo.save_node(t3)
    assert engine.resolve_task_state("AUTH-T03") == NodeStatus.WAITING_REVIEW

    with pytest.raises(ValueError, match="not found"):
        engine.resolve_task_state("NONEXISTENT")


def test_resolve_task_state_gated_dependency(
    repos: tuple[NodeRepository, RuntimeRepository, GraphEngine],
) -> None:
    node_repo, _, engine = repos

    a = Node(id="AUTH-T01", kind=NodeKind.TASK, title="Implement", status=NodeStatus.NOT_STARTED)
    b = Node(id="AUTH-T02", kind=NodeKind.TASK, title="Review", status=NodeStatus.NOT_STARTED)
    node_repo.save_node(a)
    node_repo.save_node(b)
    node_repo.add_relation(
        NodeRelation(
            source_id="AUTH-T02",
            target_id="AUTH-T01",
            relation_type=RelationType.DEPENDS_ON,
            metadata={"gate": NodeStatus.WAITING_REVIEW.value},
        )
    )

    assert engine.resolve_task_state("AUTH-T02") == VirtualStatus.BLOCKED

    a.status = NodeStatus.IMPLEMENTING
    node_repo.save_node(a)
    assert engine.resolve_task_state("AUTH-T02") == VirtualStatus.BLOCKED

    a.status = NodeStatus.WAITING_REVIEW
    node_repo.save_node(a)
    assert engine.resolve_task_state("AUTH-T02") == VirtualStatus.READY

    a.status = NodeStatus.WAITING_MERGE
    node_repo.save_node(a)
    assert engine.resolve_task_state("AUTH-T02") == VirtualStatus.READY

    a.status = NodeStatus.COMPLETED
    node_repo.save_node(a)
    assert engine.resolve_task_state("AUTH-T02") == VirtualStatus.READY


def test_resolve_task_state_bare_dependency_still_gates_on_completed(
    repos: tuple[NodeRepository, RuntimeRepository, GraphEngine],
) -> None:
    node_repo, _, engine = repos

    a = Node(id="AUTH-T01", kind=NodeKind.TASK, title="Implement", status=NodeStatus.NOT_STARTED)
    b = Node(id="AUTH-T02", kind=NodeKind.TASK, title="Merge", status=NodeStatus.NOT_STARTED)
    node_repo.save_node(a)
    node_repo.save_node(b)
    node_repo.add_relation(
        NodeRelation(
            source_id="AUTH-T02",
            target_id="AUTH-T01",
            relation_type=RelationType.DEPENDS_ON,
        )
    )

    for status in (
        NodeStatus.IMPLEMENTING,
        NodeStatus.WAITING_REVIEW,
        NodeStatus.WAITING_MERGE,
    ):
        a.status = status
        node_repo.save_node(a)
        assert engine.resolve_task_state("AUTH-T02") == VirtualStatus.BLOCKED

    a.status = NodeStatus.COMPLETED
    node_repo.save_node(a)
    assert engine.resolve_task_state("AUTH-T02") == VirtualStatus.READY


def test_resolve_task_state_in_flight(
    repos: tuple[NodeRepository, RuntimeRepository, GraphEngine],
) -> None:
    node_repo, runtime_repo, engine = repos

    t1 = Node(id="AUTH-T01", kind=NodeKind.TASK, title="Task 1", status=NodeStatus.NOT_STARTED)
    node_repo.save_node(t1)

    assert engine.resolve_task_state("AUTH-T01") == VirtualStatus.READY

    lease = Lease(
        task_id="AUTH-T01",
        agent_id="agent-worker-1",
        session_id="sess-100",
        branch_name="feature/auth",
        acquired_at=datetime.now(tz=UTC),
        last_heartbeat=datetime.now(tz=UTC),
        ttl_seconds=300,
    )
    lock = FileLock(file_path="src/auth.py", task_id="AUTH-T01")
    runtime_repo.acquire_lease(lease, [lock])

    assert engine.resolve_task_state("AUTH-T01") == VirtualStatus.IN_FLIGHT

    expired_lease = Lease(
        task_id="AUTH-T01",
        agent_id="agent-worker-1",
        session_id="sess-100",
        branch_name="feature/auth",
        acquired_at=datetime.now(tz=UTC) - timedelta(seconds=400),
        last_heartbeat=datetime.now(tz=UTC) - timedelta(seconds=350),
        ttl_seconds=60,
    )
    runtime_repo.acquire_lease(expired_lease, [lock])

    assert engine.resolve_task_state("AUTH-T01") == VirtualStatus.READY

    runtime_repo.release_lease("AUTH-T01")
    assert engine.resolve_task_state("AUTH-T01") == VirtualStatus.READY


def test_resolve_task_state_blocked_by_lease(
    repos: tuple[NodeRepository, RuntimeRepository, GraphEngine],
) -> None:
    # Every depends_on gate clear, but a file this task declares is locked by another task's
    # active lease: READY by the graph, not actually claimable -- BLOCKED_BY_LEASE, not READY
    # and not BLOCKED (BLOCKED means an unmet dependency, which this task has none of).
    node_repo, runtime_repo, engine = repos

    holder = Node(
        id="AUTH-T01", kind=NodeKind.TASK, title="Holds the lease", status=NodeStatus.NOT_STARTED
    )
    waiter = Node(
        id="AUTH-T02",
        kind=NodeKind.TASK,
        title="Wants the same file",
        status=NodeStatus.NOT_STARTED,
    )
    node_repo.save_node(holder)
    node_repo.save_node(waiter)
    node_repo.add_verification(
        NodeVerification(
            node_id="AUTH-T02",
            verification_type=VerificationType.FILE_EXISTS,
            target_path="src/shared.py",
        )
    )

    assert engine.resolve_task_state("AUTH-T02") == VirtualStatus.READY

    lease = Lease(
        task_id="AUTH-T01",
        agent_id="agent-worker-1",
        session_id="sess-100",
        branch_name="feature/shared",
        acquired_at=datetime.now(tz=UTC),
        last_heartbeat=datetime.now(tz=UTC),
        ttl_seconds=300,
    )
    runtime_repo.acquire_lease(lease, [FileLock(file_path="src/shared.py", task_id="AUTH-T01")])

    assert engine.resolve_task_state("AUTH-T02") == VirtualStatus.BLOCKED_BY_LEASE
    # The holder itself is IN_FLIGHT, not BLOCKED_BY_LEASE on its own lock.
    assert engine.resolve_task_state("AUTH-T01") == VirtualStatus.IN_FLIGHT

    runtime_repo.release_lease("AUTH-T01")
    assert engine.resolve_task_state("AUTH-T02") == VirtualStatus.READY


def test_cycle_detection(
    repos: tuple[NodeRepository, RuntimeRepository, GraphEngine],
) -> None:
    node_repo, _, engine = repos

    assert engine.would_cause_cycle("A", "A") is True

    node_repo.save_node(Node(id="A", kind=NodeKind.TASK, title="A"))
    node_repo.save_node(Node(id="B", kind=NodeKind.TASK, title="B"))
    node_repo.save_node(Node(id="C", kind=NodeKind.TASK, title="C"))
    node_repo.save_node(Node(id="D", kind=NodeKind.TASK, title="D"))
    node_repo.save_node(Node(id="E", kind=NodeKind.TASK, title="E"))

    node_repo.add_relation(
        NodeRelation(source_id="A", target_id="B", relation_type=RelationType.DEPENDS_ON)
    )
    node_repo.add_relation(
        NodeRelation(source_id="B", target_id="C", relation_type=RelationType.DEPENDS_ON)
    )

    assert engine.would_cause_cycle("C", "A") is True
    assert engine.would_cause_cycle("B", "A") is True
    assert engine.would_cause_cycle("A", "C") is False
    assert engine.would_cause_cycle("D", "A") is False

    node_repo.add_relation(
        NodeRelation(source_id="A", target_id="D", relation_type=RelationType.DEPENDS_ON)
    )
    node_repo.add_relation(
        NodeRelation(source_id="D", target_id="E", relation_type=RelationType.DEPENDS_ON)
    )

    assert engine.would_cause_cycle("E", "A") is True
    assert engine.would_cause_cycle("A", "E") is False


def test_plan_status_rollup(
    repos: tuple[NodeRepository, RuntimeRepository, GraphEngine],
) -> None:
    node_repo, runtime_repo, engine = repos

    plan = Node(id="AUTH-P01", kind=NodeKind.PLAN, title="Plan", status=NodeStatus.NOT_STARTED)
    node_repo.save_node(plan)

    assert engine.resolve_plan_status("AUTH-P01") == NodeStatus.NOT_STARTED

    t1 = Node(id="AUTH-T01", kind=NodeKind.TASK, title="Task 1", status=NodeStatus.NOT_STARTED)
    t2 = Node(id="AUTH-T02", kind=NodeKind.TASK, title="Task 2", status=NodeStatus.NOT_STARTED)
    node_repo.save_node(t1)
    node_repo.save_node(t2)

    node_repo.add_relation(
        NodeRelation(
            source_id="AUTH-P01", target_id="AUTH-T01", relation_type=RelationType.CONTAINS
        )
    )
    node_repo.add_relation(
        NodeRelation(
            source_id="AUTH-P01", target_id="AUTH-T02", relation_type=RelationType.CONTAINS
        )
    )

    assert engine.resolve_plan_status("AUTH-P01") == NodeStatus.NOT_STARTED

    node_repo.add_relation(
        NodeRelation(
            source_id="AUTH-T02", target_id="AUTH-T01", relation_type=RelationType.DEPENDS_ON
        )
    )
    assert engine.resolve_plan_status("AUTH-P01") == NodeStatus.NOT_STARTED

    ext = Node(
        id="EXT-T00", kind=NodeKind.TASK, title="External Task", status=NodeStatus.NOT_STARTED
    )
    node_repo.save_node(ext)
    node_repo.add_relation(
        NodeRelation(
            source_id="AUTH-T01", target_id="EXT-T00", relation_type=RelationType.DEPENDS_ON
        )
    )
    assert engine.resolve_plan_status("AUTH-P01") == VirtualStatus.BLOCKED

    ext.status = NodeStatus.COMPLETED
    node_repo.save_node(ext)
    assert engine.resolve_plan_status("AUTH-P01") == NodeStatus.NOT_STARTED

    t1.status = NodeStatus.IMPLEMENTING
    node_repo.save_node(t1)
    assert engine.resolve_plan_status("AUTH-P01") == NodeStatus.IMPLEMENTING

    t1.status = NodeStatus.COMPLETED
    node_repo.save_node(t1)
    assert engine.resolve_plan_status("AUTH-P01") == NodeStatus.IMPLEMENTING

    node_repo.add_relation(
        NodeRelation(
            source_id="AUTH-T02", target_id="EXT-T00", relation_type=RelationType.DEPENDS_ON
        )
    )
    ext.status = NodeStatus.IMPLEMENTING
    node_repo.save_node(ext)
    assert engine.resolve_plan_status("AUTH-P01") == VirtualStatus.BLOCKED

    ext.status = NodeStatus.COMPLETED
    node_repo.save_node(ext)
    assert engine.resolve_plan_status("AUTH-P01") == NodeStatus.IMPLEMENTING

    lease = Lease(
        task_id="AUTH-T02",
        agent_id="agent-1",
        session_id="sess-1",
        branch_name="feature/auth",
        acquired_at=datetime.now(tz=UTC),
        last_heartbeat=datetime.now(tz=UTC),
        ttl_seconds=300,
    )
    runtime_repo.acquire_lease(lease, [])
    assert engine.resolve_plan_status("AUTH-P01") == NodeStatus.IMPLEMENTING
    runtime_repo.release_lease("AUTH-T02")

    t2.status = NodeStatus.COMPLETED
    node_repo.save_node(t2)
    assert engine.resolve_plan_status("AUTH-P01") == NodeStatus.COMPLETED

    with pytest.raises(ValueError, match="not found"):
        engine.resolve_plan_status("NONEXISTENT")


def test_inject_plan_review_gate(
    repos: tuple[NodeRepository, RuntimeRepository, GraphEngine],
) -> None:
    node_repo, _, engine = repos

    plan = Node(
        id="AUTH-P01",
        kind=NodeKind.PLAN,
        title="Auth Plan",
        target_repo="backend",
        status=NodeStatus.NOT_STARTED,
    )
    node_repo.save_node(plan)

    t1 = Node(id="AUTH-T01", kind=NodeKind.TASK, title="Task 1", status=NodeStatus.NOT_STARTED)
    t2 = Node(id="AUTH-T02", kind=NodeKind.TASK, title="Task 2", status=NodeStatus.NOT_STARTED)
    node_repo.save_node(t1)
    node_repo.save_node(t2)

    node_repo.add_relation(
        NodeRelation(
            source_id="AUTH-P01", target_id="AUTH-T01", relation_type=RelationType.CONTAINS
        )
    )
    node_repo.add_relation(
        NodeRelation(
            source_id="AUTH-P01", target_id="AUTH-T02", relation_type=RelationType.CONTAINS
        )
    )

    gate_id = engine.inject_plan_review_gate("AUTH-P01")
    assert gate_id == "AUTH-P01-REV"

    gate_node = node_repo.get_node(gate_id)
    assert gate_node is not None
    assert gate_node.kind == NodeKind.REVIEW_GATE
    assert gate_node.status == NodeStatus.NOT_STARTED
    assert gate_node.target_repo == "backend"

    deps = node_repo.get_dependencies(gate_id)
    assert set(deps) == {"AUTH-T01", "AUTH-T02"}

    assert engine.resolve_task_state(gate_id) == VirtualStatus.BLOCKED

    t1.status = NodeStatus.COMPLETED
    node_repo.save_node(t1)
    assert engine.resolve_task_state(gate_id) == VirtualStatus.BLOCKED

    t2.status = NodeStatus.COMPLETED
    node_repo.save_node(t2)
    assert engine.resolve_task_state(gate_id) == VirtualStatus.READY
