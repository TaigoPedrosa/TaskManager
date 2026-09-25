from datetime import UTC, datetime
from pathlib import Path

import pytest

from taskmanager.core.enums import (
    NodeKind,
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
from taskmanager.core.status import Action, Status
from taskmanager.db.connection import DatabaseManager
from taskmanager.db.job_repo import JobRepository
from taskmanager.db.node_repo import NodeRepository
from taskmanager.db.runtime_repo import RuntimeRepository
from taskmanager.engine.heuristics import RecommendationEngine, ScoredTask
from taskmanager.engine.snapshot import SnapshotBuilder


@pytest.fixture
def env(
    tmp_path: Path,
) -> tuple[NodeRepository, RuntimeRepository, SnapshotBuilder, RecommendationEngine]:
    db = DatabaseManager(tmp_path)
    db.init_all()
    node_repo = NodeRepository(db)
    runtime_repo = RuntimeRepository(db)
    snapshots = SnapshotBuilder(node_repo, runtime_repo, JobRepository(db))
    engine = RecommendationEngine(node_repo, runtime_repo, snapshots)
    return node_repo, runtime_repo, snapshots, engine


def _claim(
    node_repo: NodeRepository, runtime_repo: RuntimeRepository, node_id: str, *files: str
) -> None:
    node = node_repo.get_node(node_id)
    assert node is not None
    node.status, node.claimed_from = Status.IMPLEMENTING, Status.READY
    lease = Lease(
        task_id=node_id,
        agent_id="agent-1",
        session_id="sess-1",
        branch_name=f"tm/{node_id}",
        action=Action.IMPLEMENT,
        acquired_at=datetime.now(tz=UTC),
        last_heartbeat=datetime.now(tz=UTC),
        ttl_seconds=300,
    )
    locks = [FileLock(file_path=f, task_id=node_id) for f in files]
    assert runtime_repo.claim(lease, locks, node)


def test_recommendation_scoring_and_ranking_of_ready_tasks(
    env: tuple[NodeRepository, RuntimeRepository, SnapshotBuilder, RecommendationEngine],
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


def test_exclusion_of_blocked_claimed_and_already_implemented_tasks(
    env: tuple[NodeRepository, RuntimeRepository, SnapshotBuilder, RecommendationEngine],
) -> None:
    node_repo, runtime_repo, _, engine = env
    node_repo.save_node(Node(id="T-01", kind=NodeKind.TASK, title="Prerequisite", priority=80))
    node_repo.save_node(Node(id="T-02", kind=NodeKind.TASK, title="Blocked", priority=90))
    node_repo.save_node(Node(id="T-03", kind=NodeKind.TASK, title="Claimed", priority=85))
    node_repo.save_node(
        Node(
            id="T-04",
            kind=NodeKind.TASK,
            title="Already implemented",
            priority=70,
            status=Status.IMPLEMENTED,
        )
    )
    node_repo.add_relation(
        NodeRelation(source_id="T-02", target_id="T-01", relation_type=RelationType.DEPENDS_ON)
    )
    _claim(node_repo, runtime_repo, "T-03")

    assert [t.task_id for t in engine.get_next_tasks(limit=10)] == ["T-01"]


def test_exclusion_of_file_colliding_tasks(
    env: tuple[NodeRepository, RuntimeRepository, SnapshotBuilder, RecommendationEngine],
) -> None:
    node_repo, runtime_repo, _, engine = env
    node_repo.save_node(
        Node(id="TASK-VER", kind=NodeKind.TASK, title="Has Verification File", priority=80)
    )
    node_repo.save_node(
        Node(
            id="TASK-FM",
            kind=NodeKind.TASK,
            title="Has Frontmatter File",
            priority=75,
            frontmatter={"declared_files": ["src/db.py"]},
        )
    )
    node_repo.save_node(
        Node(id="TASK-FREE", kind=NodeKind.TASK, title="Free of locks", priority=70)
    )
    node_repo.save_node(Node(id="OTHER-TASK", kind=NodeKind.TASK, title="Holds files", priority=1))
    node_repo.add_verification(
        NodeVerification(
            node_id="TASK-VER",
            verification_type=VerificationType.FILE_EXISTS,
            target_path="src/auth/jwt.py",
        )
    )

    assert {t.task_id for t in engine.get_next_tasks(limit=5)} == {
        "TASK-VER",
        "TASK-FM",
        "TASK-FREE",
        "OTHER-TASK",
    }

    _claim(node_repo, runtime_repo, "OTHER-TASK", "src/auth/jwt.py", "src/db.py")
    assert [t.task_id for t in engine.get_next_tasks(limit=5)] == ["TASK-FREE"]

    runtime_repo.release_lease("OTHER-TASK")
    assert {t.task_id for t in engine.get_next_tasks(limit=5)} == {
        "TASK-VER",
        "TASK-FM",
        "TASK-FREE",
    }


def test_strategy_overrides(
    env: tuple[NodeRepository, RuntimeRepository, SnapshotBuilder, RecommendationEngine],
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
        status=Status.COMPLETED,
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
    env: tuple[NodeRepository, RuntimeRepository, SnapshotBuilder, RecommendationEngine],
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
    env: tuple[NodeRepository, RuntimeRepository, SnapshotBuilder, RecommendationEngine],
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


def test_spec_id_filter(
    env: tuple[NodeRepository, RuntimeRepository, SnapshotBuilder, RecommendationEngine],
) -> None:
    node_repo, _, _, engine = env

    node_repo.save_node(Node(id="SPEC-A", kind=NodeKind.SPEC, title="Spec A"))
    node_repo.save_node(Node(id="SPEC-B", kind=NodeKind.SPEC, title="Spec B"))
    node_repo.save_node(Node(id="A-PLAN", kind=NodeKind.PLAN, title="A Plan"))
    node_repo.save_node(Node(id="B-PLAN", kind=NodeKind.PLAN, title="B Plan"))
    node_repo.save_node(Node(id="ORPHAN-PLAN", kind=NodeKind.PLAN, title="Orphan Plan"))
    node_repo.add_relation(
        NodeRelation(source_id="SPEC-A", target_id="A-PLAN", relation_type=RelationType.CONTAINS)
    )
    node_repo.add_relation(
        NodeRelation(source_id="SPEC-B", target_id="B-PLAN", relation_type=RelationType.CONTAINS)
    )

    node_repo.save_node(Node(id="A-T1", kind=NodeKind.TASK, title="A Task"))
    node_repo.add_relation(
        NodeRelation(source_id="A-PLAN", target_id="A-T1", relation_type=RelationType.CONTAINS)
    )
    node_repo.save_node(Node(id="B-T1", kind=NodeKind.TASK, title="B Task"))
    node_repo.add_relation(
        NodeRelation(source_id="B-PLAN", target_id="B-T1", relation_type=RelationType.CONTAINS)
    )
    node_repo.save_node(Node(id="ORPHAN-T1", kind=NodeKind.TASK, title="Plan, no spec"))
    node_repo.add_relation(
        NodeRelation(
            source_id="ORPHAN-PLAN", target_id="ORPHAN-T1", relation_type=RelationType.CONTAINS
        )
    )
    node_repo.save_node(Node(id="NO-PLAN-T1", kind=NodeKind.TASK, title="No plan at all"))

    spec_a = [t.task_id for t in engine.get_next_tasks(spec_id="SPEC-A", limit=10)]
    assert spec_a == ["A-T1"]

    none_spec = {t.task_id for t in engine.get_next_tasks(spec_id="none", limit=10)}
    assert none_spec == {"ORPHAN-T1", "NO-PLAN-T1"}

    every_task = {t.task_id for t in engine.get_next_tasks(limit=10)}
    assert every_task == {"A-T1", "B-T1", "ORPHAN-T1", "NO-PLAN-T1"}


def test_spec_id_filter_walks_nested_plans(
    env: tuple[NodeRepository, RuntimeRepository, SnapshotBuilder, RecommendationEngine],
) -> None:
    """A plan `add`ed with another plan's id as its `--spec` nests under that plan instead of a
    spec (`Operations.add_plan` never checks the parent's kind); the filter has to walk past it."""
    node_repo, _, _, engine = env

    node_repo.save_node(Node(id="SPEC-A", kind=NodeKind.SPEC, title="Spec A"))
    node_repo.save_node(Node(id="OUTER-PLAN", kind=NodeKind.PLAN, title="Outer"))
    node_repo.save_node(Node(id="INNER-PLAN", kind=NodeKind.PLAN, title="Inner"))
    node_repo.add_relation(
        NodeRelation(
            source_id="SPEC-A", target_id="OUTER-PLAN", relation_type=RelationType.CONTAINS
        )
    )
    node_repo.add_relation(
        NodeRelation(
            source_id="OUTER-PLAN", target_id="INNER-PLAN", relation_type=RelationType.CONTAINS
        )
    )
    node_repo.save_node(Node(id="NESTED-T1", kind=NodeKind.TASK, title="Nested Task"))
    node_repo.add_relation(
        NodeRelation(
            source_id="INNER-PLAN", target_id="NESTED-T1", relation_type=RelationType.CONTAINS
        )
    )

    spec_a = [t.task_id for t in engine.get_next_tasks(spec_id="SPEC-A", limit=10)]
    assert spec_a == ["NESTED-T1"]

    none_spec = [t.task_id for t in engine.get_next_tasks(spec_id="none", limit=10)]
    assert none_spec == []


def test_get_next_tasks_never_offers_a_task_awaiting_decision(
    env: tuple[NodeRepository, RuntimeRepository, SnapshotBuilder, RecommendationEngine],
) -> None:
    node_repo, _runtime_repo, _graph, engine = env
    waiting = Node(id="T-WAIT", kind=NodeKind.TASK, title="Waiting")
    ready = Node(id="T-READY", kind=NodeKind.TASK, title="Ready")
    decision = Node(id="decision-D1", kind=NodeKind.DECISION, title="Which way?")
    node_repo.save_node(waiting)
    node_repo.save_node(ready)
    node_repo.save_node(decision)
    node_repo.add_relation(
        NodeRelation(
            source_id="T-WAIT", target_id="decision-D1", relation_type=RelationType.DEPENDS_ON
        )
    )

    ranked = engine.get_next_tasks(limit=10)
    assert {t.task_id for t in ranked} == {"T-READY"}


def test_score_every_task_ignores_a_decision_node(
    env: tuple[NodeRepository, RuntimeRepository, SnapshotBuilder, RecommendationEngine],
) -> None:
    from taskmanager.engine.heuristics import score_every_task

    node_repo, _runtime_repo, _graph, _engine = env
    node_repo.save_node(Node(id="T1", kind=NodeKind.TASK, title="Task"))
    node_repo.save_node(Node(id="decision-D1", kind=NodeKind.DECISION, title="Which way?"))

    scores = score_every_task(node_repo)
    assert set(scores) == {"T1"}
