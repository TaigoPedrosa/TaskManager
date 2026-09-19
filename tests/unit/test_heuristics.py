from datetime import UTC, datetime
from pathlib import Path

import pytest

from taskmanager.core.enums import (
    NodeKind,
    NodeStatus,
    RelationType,
    VerificationType,
)
from taskmanager.core.models import (
    FileLock,
    Lease,
    Node,
    NodeRelation,
    NodeVerification,
)
from taskmanager.db.connection import DatabaseManager
from taskmanager.db.node_repo import NodeRepository
from taskmanager.db.runtime_repo import RuntimeRepository
from taskmanager.engine.graph import GraphEngine
from taskmanager.engine.heuristics import RecommendationEngine, ScoredTask


@pytest.fixture
def env(
    tmp_path: Path,
) -> tuple[NodeRepository, RuntimeRepository, GraphEngine, RecommendationEngine]:
    db = DatabaseManager(tmp_path)
    db.init_all()
    node_repo = NodeRepository(db)
    runtime_repo = RuntimeRepository(db)
    graph = GraphEngine(node_repo, runtime_repo)
    engine = RecommendationEngine(node_repo, runtime_repo, graph)
    return node_repo, runtime_repo, graph, engine


def test_recommendation_scoring_and_ranking_of_ready_tasks(
    env: tuple[NodeRepository, RuntimeRepository, GraphEngine, RecommendationEngine],
) -> None:
    node_repo, _, _, engine = env

    plan = Node(id="AUTH-P01", kind=NodeKind.PLAN, title="Auth Plan", priority=100)
    node_repo.save_node(plan)

    t1 = Node(
        id="AUTH-T01",
        kind=NodeKind.TASK,
        title="Implement OAuth",
        priority=90,
        acceptable_models=["claude-3-7-sonnet"],
    )
    t2 = Node(
        id="AUTH-T02",
        kind=NodeKind.TASK,
        title="Add Token Storage",
        priority=50,
        acceptable_models=["gemini-2.5-flash"],
    )
    node_repo.save_node(t1)
    node_repo.save_node(t2)

    node_repo.add_relation(
        NodeRelation(
            source_id="AUTH-P01",
            target_id="AUTH-T01",
            relation_type=RelationType.CONTAINS,
        )
    )
    node_repo.add_relation(
        NodeRelation(
            source_id="AUTH-P01",
            target_id="AUTH-T02",
            relation_type=RelationType.CONTAINS,
        )
    )
    node_repo.add_relation(
        NodeRelation(
            source_id="AUTH-T02",
            target_id="AUTH-T01",
            relation_type=RelationType.DEPENDS_ON,
        )
    )

    ranked = engine.get_next_tasks(limit=5)
    assert len(ranked) == 1
    scored_t1: ScoredTask = ranked[0]
    assert scored_t1.task_id == "AUTH-T01"
    assert scored_t1.title == "Implement OAuth"
    assert scored_t1.plan_id == "AUTH-P01"
    assert scored_t1.priority == 90
    assert scored_t1.acceptable_models == ["claude-3-7-sonnet"]
    assert scored_t1.unblocking_count == 1
    assert scored_t1.declared_files == []
    assert scored_t1.score == 40.05


def test_exclusion_of_blocked_in_flight_and_non_not_started_tasks(
    env: tuple[NodeRepository, RuntimeRepository, GraphEngine, RecommendationEngine],
) -> None:
    node_repo, runtime_repo, _, engine = env

    t1 = Node(id="T-01", kind=NodeKind.TASK, title="Prerequisite", priority=80)
    t2 = Node(id="T-02", kind=NodeKind.TASK, title="Blocked", priority=90)
    t3 = Node(id="T-03", kind=NodeKind.TASK, title="In Flight", priority=85)
    t4 = Node(
        id="T-04",
        kind=NodeKind.TASK,
        title="Already in review",
        priority=70,
        status=NodeStatus.WAITING_REVIEW,
    )
    node_repo.save_node(t1)
    node_repo.save_node(t2)
    node_repo.save_node(t3)
    node_repo.save_node(t4)

    node_repo.add_relation(
        NodeRelation(source_id="T-02", target_id="T-01", relation_type=RelationType.DEPENDS_ON)
    )

    lease = Lease(
        task_id="T-03",
        agent_id="agent-1",
        session_id="sess-1",
        branch_name="feature/t3",
        acquired_at=datetime.now(tz=UTC),
        last_heartbeat=datetime.now(tz=UTC),
        ttl_seconds=300,
    )
    runtime_repo.acquire_lease(lease, [])

    ranked = engine.get_next_tasks(limit=10)
    task_ids = [t.task_id for t in ranked]
    assert task_ids == ["T-01"]


def test_exclusion_of_file_colliding_tasks(
    env: tuple[NodeRepository, RuntimeRepository, GraphEngine, RecommendationEngine],
) -> None:
    node_repo, runtime_repo, _, engine = env

    t1 = Node(id="TASK-VER", kind=NodeKind.TASK, title="Has Verification File", priority=80)
    t2 = Node(
        id="TASK-FM",
        kind=NodeKind.TASK,
        title="Has Frontmatter File",
        priority=75,
        frontmatter={"declared_files": ["src/db.py"]},
    )
    t3 = Node(id="TASK-FREE", kind=NodeKind.TASK, title="Free of locks", priority=70)
    node_repo.save_node(t1)
    node_repo.save_node(t2)
    node_repo.save_node(t3)

    node_repo.add_verification(
        NodeVerification(
            node_id="TASK-VER",
            verification_type=VerificationType.FILE_EXISTS,
            target_path="src/auth/jwt.py",
        )
    )

    ranked_initial = engine.get_next_tasks(limit=5)
    assert {t.task_id for t in ranked_initial} == {"TASK-VER", "TASK-FM", "TASK-FREE"}

    lease = Lease(
        task_id="OTHER-TASK",
        agent_id="agent-worker",
        session_id="sess-99",
        branch_name="feature/other",
        acquired_at=datetime.now(tz=UTC),
        last_heartbeat=datetime.now(tz=UTC),
        ttl_seconds=300,
    )
    lock1 = FileLock(file_path="src/auth/jwt.py", task_id="OTHER-TASK")
    lock2 = FileLock(file_path="src/db.py", task_id="OTHER-TASK")
    runtime_repo.acquire_lease(lease, [lock1, lock2])

    ranked_colliding = engine.get_next_tasks(limit=5)
    assert [t.task_id for t in ranked_colliding] == ["TASK-FREE"]

    runtime_repo.release_lease("OTHER-TASK")
    ranked_after_release = engine.get_next_tasks(limit=5)
    assert {t.task_id for t in ranked_after_release} == {"TASK-VER", "TASK-FM", "TASK-FREE"}


def test_strategy_overrides(
    env: tuple[NodeRepository, RuntimeRepository, GraphEngine, RecommendationEngine],
) -> None:
    node_repo, _, _, engine = env

    plan_a = Node(id="PLAN-A", kind=NodeKind.PLAN, title="Plan A", priority=100)
    node_repo.save_node(plan_a)
    task_prio = Node(id="TASK-PRIO", kind=NodeKind.TASK, title="High Priority Task", priority=100)
    task_a_dummy = Node(id="TASK-A-DUMMY", kind=NodeKind.TASK, title="Dummy Task", priority=10)
    node_repo.save_node(task_prio)
    node_repo.save_node(task_a_dummy)
    node_repo.add_relation(
        NodeRelation(source_id="PLAN-A", target_id="TASK-PRIO", relation_type=RelationType.CONTAINS)
    )
    node_repo.add_relation(
        NodeRelation(
            source_id="PLAN-A", target_id="TASK-A-DUMMY", relation_type=RelationType.CONTAINS
        )
    )

    plan_b = Node(id="PLAN-B", kind=NodeKind.PLAN, title="Plan B", priority=30)
    node_repo.save_node(plan_b)
    task_unlock = Node(id="TASK-UNLOCK", kind=NodeKind.TASK, title="Unblocking Task", priority=30)
    node_repo.save_node(task_unlock)
    node_repo.add_relation(
        NodeRelation(
            source_id="PLAN-B", target_id="TASK-UNLOCK", relation_type=RelationType.CONTAINS
        )
    )
    for i in range(4):
        downstream = Node(id=f"DOWN-{i}", kind=NodeKind.TASK, title=f"Downstream {i}", priority=20)
        node_repo.save_node(downstream)
        node_repo.add_relation(
            NodeRelation(
                source_id=f"DOWN-{i}",
                target_id="TASK-UNLOCK",
                relation_type=RelationType.DEPENDS_ON,
            )
        )

    plan_c = Node(id="PLAN-C", kind=NodeKind.PLAN, title="Plan C", priority=50)
    node_repo.save_node(plan_c)
    task_c_done = Node(
        id="TASK-C-DONE",
        kind=NodeKind.TASK,
        title="Completed Subtask",
        priority=50,
        status=NodeStatus.COMPLETED,
    )
    task_finish = Node(id="TASK-FINISH", kind=NodeKind.TASK, title="Closing Task", priority=50)
    node_repo.save_node(task_c_done)
    node_repo.save_node(task_finish)
    node_repo.add_relation(
        NodeRelation(
            source_id="PLAN-C", target_id="TASK-C-DONE", relation_type=RelationType.CONTAINS
        )
    )
    node_repo.add_relation(
        NodeRelation(
            source_id="PLAN-C", target_id="TASK-FINISH", relation_type=RelationType.CONTAINS
        )
    )

    prio_ranked = engine.get_next_tasks(strategy="priority-strict", limit=3)
    assert prio_ranked[0].task_id == "TASK-PRIO"
    assert prio_ranked[0].score == 100.0

    unlock_ranked = engine.get_next_tasks(strategy="unblock-first", limit=3)
    assert unlock_ranked[0].task_id == "TASK-UNLOCK"

    finish_ranked = engine.get_next_tasks(strategy="finish-plans", limit=3)
    assert finish_ranked[0].task_id == "TASK-FINISH"


def test_model_filtering(
    env: tuple[NodeRepository, RuntimeRepository, GraphEngine, RecommendationEngine],
) -> None:
    node_repo, _, _, engine = env

    t1 = Node(
        id="T-SONNET",
        kind=NodeKind.TASK,
        title="Sonnet Only",
        priority=80,
        acceptable_models=["claude-3-7-sonnet"],
    )
    t2 = Node(
        id="T-FLASH",
        kind=NodeKind.TASK,
        title="Flash Only",
        priority=80,
        acceptable_models=["gemini-2.5-flash"],
    )
    t3 = Node(
        id="T-ANY",
        kind=NodeKind.TASK,
        title="Any Model",
        priority=80,
        acceptable_models=[],
    )
    node_repo.save_node(t1)
    node_repo.save_node(t2)
    node_repo.save_node(t3)

    sonnet_tasks = [t.task_id for t in engine.get_next_tasks(model_filter="claude-3-7-sonnet")]
    assert "T-SONNET" in sonnet_tasks
    assert "T-ANY" in sonnet_tasks
    assert "T-FLASH" not in sonnet_tasks

    flash_tasks = [t.task_id for t in engine.get_next_tasks(model_filter="gemini-2.5-flash")]
    assert "T-FLASH" in flash_tasks
    assert "T-ANY" in flash_tasks
    assert "T-SONNET" not in flash_tasks

    other_tasks = [t.task_id for t in engine.get_next_tasks(model_filter="gpt-4o")]
    assert other_tasks == ["T-ANY"]

    all_tasks = [t.task_id for t in engine.get_next_tasks(model_filter=None)]
    assert set(all_tasks) == {"T-SONNET", "T-FLASH", "T-ANY"}


def test_plan_id_filter_and_limit(
    env: tuple[NodeRepository, RuntimeRepository, GraphEngine, RecommendationEngine],
) -> None:
    node_repo, _, _, engine = env

    p1 = Node(id="PLAN-1", kind=NodeKind.PLAN, title="Plan 1", priority=50)
    p2 = Node(id="PLAN-2", kind=NodeKind.PLAN, title="Plan 2", priority=50)
    node_repo.save_node(p1)
    node_repo.save_node(p2)

    for i in range(5):
        t = Node(id=f"P1-T{i}", kind=NodeKind.TASK, title=f"P1 Task {i}", priority=60 + i)
        node_repo.save_node(t)
        node_repo.add_relation(
            NodeRelation(source_id="PLAN-1", target_id=t.id, relation_type=RelationType.CONTAINS)
        )

    for i in range(3):
        t = Node(id=f"P2-T{i}", kind=NodeKind.TASK, title=f"P2 Task {i}", priority=70 + i)
        node_repo.save_node(t)
        node_repo.add_relation(
            NodeRelation(source_id="PLAN-2", target_id=t.id, relation_type=RelationType.CONTAINS)
        )

    p1_only = engine.get_next_tasks(plan_id="PLAN-1", limit=10)
    assert len(p1_only) == 5
    assert all(t.plan_id == "PLAN-1" for t in p1_only)

    limited = engine.get_next_tasks(limit=2)
    assert len(limited) == 2
