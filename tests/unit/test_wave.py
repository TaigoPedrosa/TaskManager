import json
from pathlib import Path

import pytest

from taskmanager.core.enums import NodeKind, NodeStatus, RelationType
from taskmanager.core.models import Lease, Node, NodeRelation, NodeSection
from taskmanager.db.connection import DatabaseManager
from taskmanager.db.ledger_repo import LedgerRepository
from taskmanager.db.node_repo import NodeRepository
from taskmanager.db.runtime_repo import RuntimeRepository
from taskmanager.engine.graph import GraphEngine
from taskmanager.engine.heuristics import RecommendationEngine
from taskmanager.engine.operations import Operations
from taskmanager.engine.runtime import ExecutionCoordinator
from taskmanager.engine.verification import VerificationEngine
from taskmanager.engine.wave import discover_batch, djb2


@pytest.fixture
def env(
    tmp_path: Path,
) -> tuple[NodeRepository, RuntimeRepository, Operations, RecommendationEngine]:
    db = DatabaseManager(tmp_path)
    db.init_all()
    node_repo = NodeRepository(db)
    runtime_repo = RuntimeRepository(db)
    ledger_repo = LedgerRepository(db)
    graph = GraphEngine(node_repo, runtime_repo)
    coordinator = ExecutionCoordinator(node_repo, runtime_repo, graph, git_mgr=None)
    verification_engine = VerificationEngine(tmp_path)
    ops = Operations(
        node_repo, runtime_repo, graph, coordinator, ledger_repo, verification_engine, actor="t"
    )
    heuristics = RecommendationEngine(node_repo, runtime_repo, graph)
    return node_repo, runtime_repo, ops, heuristics


def _spec_plan(node_repo: NodeRepository, spec_id: str = "S1", plan_id: str = "S1-P1") -> str:
    node_repo.save_node(Node(id=spec_id, kind=NodeKind.SPEC, title="Spec"))
    node_repo.save_node(Node(id=plan_id, kind=NodeKind.PLAN, title="Plan"))
    node_repo.add_relation(
        NodeRelation(source_id=spec_id, target_id=plan_id, relation_type=RelationType.CONTAINS)
    )
    return plan_id


def _task(
    node_repo: NodeRepository,
    plan_id: str,
    task_id: str,
    *,
    status: NodeStatus = NodeStatus.NOT_STARTED,
    priority: int = 50,
    repo: str | None = None,
    models: list[str] | None = None,
    frontmatter: dict[str, object] | None = None,
) -> str:
    node_repo.save_node(
        Node(
            id=task_id,
            kind=NodeKind.TASK,
            title=task_id,
            status=status,
            priority=priority,
            target_repo=repo,
            acceptable_models=models or [],
            frontmatter=frontmatter or {},
        )
    )
    node_repo.add_relation(
        NodeRelation(source_id=plan_id, target_id=task_id, relation_type=RelationType.CONTAINS)
    )
    return task_id


def test_djb2_matches_a_hand_computed_value() -> None:
    assert djb2("") == 5381
    assert djb2("a") == (5381 * 33 + ord("a")) & 0xFFFFFFFF


def test_djb2_changes_when_the_payload_changes() -> None:
    assert djb2('{"a":1}') != djb2('{"a":2}')


def test_free_slots_come_from_the_sessions_own_leases(
    env: tuple[NodeRepository, RuntimeRepository, Operations, RecommendationEngine],
) -> None:
    node_repo, runtime_repo, ops, heuristics = env
    plan = _spec_plan(node_repo)
    _task(node_repo, plan, "S1-P1-T1", priority=90)
    _task(node_repo, plan, "S1-P1-T2", priority=80)
    runtime_repo.acquire_lease(
        Lease(
            task_id="OTHER-T1",
            agent_id="wf-impl-sonnet-OTHER-T1",
            session_id="sess-1",
            branch_name="tm/OTHER-T1",
        ),
        [],
    )

    payload, n = discover_batch(node_repo, runtime_repo, ops, heuristics, ["S1"], "sess-1", 1, 5)
    data = json.loads(payload)

    assert n == 0
    assert data["chosen"] == []
    assert data["mine"] == 1
    assert data["waiting_for_slot"] == 2


def test_strong_model_cap_limits_opus_and_fable_leases(
    env: tuple[NodeRepository, RuntimeRepository, Operations, RecommendationEngine],
) -> None:
    node_repo, runtime_repo, ops, heuristics = env
    plan = _spec_plan(node_repo)
    _task(node_repo, plan, "S1-P1-T1", priority=90, models=["claude-opus-5"])
    _task(node_repo, plan, "S1-P1-T2", priority=50, models=["claude-opus-5"])

    payload, n = discover_batch(node_repo, runtime_repo, ops, heuristics, ["S1"], "sess-1", 5, 1)
    data = json.loads(payload)

    assert n == 1
    assert data["chosen"][0]["id"] == "S1-P1-T1"
    assert any(h.startswith("S1-P1-T2:") and "no free opus/fable slot" in h for h in data["held"])


def test_declared_file_overlap_holds_the_second_task_this_wave(
    env: tuple[NodeRepository, RuntimeRepository, Operations, RecommendationEngine],
) -> None:
    # Both tasks come in via different candidate sources (one already WAITING_REVIEW, one still
    # READY) so `get_next_tasks`'s own same-wave dedup can't be what closes the overlap here --
    # only `discover_batch`'s own `taken_files` guard sees both and can hold the second.
    node_repo, runtime_repo, ops, heuristics = env
    plan = _spec_plan(node_repo)
    _task(
        node_repo,
        plan,
        "S1-P1-T1",
        status=NodeStatus.WAITING_REVIEW,
        priority=90,
        frontmatter={"declared_files": ["src/shared.py"]},
    )
    _task(
        node_repo, plan, "S1-P1-T2", priority=50, frontmatter={"declared_files": ["src/shared.py"]}
    )

    payload, n = discover_batch(node_repo, runtime_repo, ops, heuristics, ["S1"], "sess-1", 5, 5)
    data = json.loads(payload)

    assert n == 1
    assert data["chosen"][0]["id"] == "S1-P1-T1"
    assert any(h.startswith("S1-P1-T2:") and "declared_files overlap" in h for h in data["held"])


def test_migration_writer_holds_its_repos_chain_for_a_ready_sibling(
    env: tuple[NodeRepository, RuntimeRepository, Operations, RecommendationEngine],
) -> None:
    node_repo, runtime_repo, ops, heuristics = env
    plan = _spec_plan(node_repo)
    _task(
        node_repo,
        plan,
        "S1-P1-T1",
        status=NodeStatus.WAITING_REVIEW,
        repo="core",
        frontmatter={"declared_files": ["src/core/migrations/versions/0001_x.py"]},
    )
    _task(
        node_repo,
        plan,
        "S1-P1-T2",
        priority=90,
        repo="core",
        frontmatter={"declared_files": ["src/core/migrations/versions/0002_y.py"]},
    )

    payload, n = discover_batch(node_repo, runtime_repo, ops, heuristics, ["S1"], "sess-1", 5, 5)
    data = json.loads(payload)

    assert n == 1
    assert data["chosen"][0]["id"] == "S1-P1-T1"
    assert any(
        h.startswith("S1-P1-T2:") and "core migration chain held by S1-P1-T1" in h
        for h in data["held"]
    )


def test_hold_section_holds_a_task_until_released(
    env: tuple[NodeRepository, RuntimeRepository, Operations, RecommendationEngine],
) -> None:
    node_repo, runtime_repo, ops, heuristics = env
    plan = _spec_plan(node_repo)
    _task(node_repo, plan, "S1-P1-T1", priority=90)
    node_repo.save_section(
        NodeSection(
            node_id="S1-P1-T1", section_key="hold", ordinal=1, header="## Hold", content="x"
        )
    )

    payload, n = discover_batch(node_repo, runtime_repo, ops, heuristics, ["S1"], "sess-1", 5, 5)
    data = json.loads(payload)
    assert n == 0
    assert any(h.startswith("S1-P1-T1:") and "hold section" in h for h in data["held"])

    payload2, n2 = discover_batch(
        node_repo, runtime_repo, ops, heuristics, ["S1"], "sess-1", 5, 5, release=["S1-P1-T1"]
    )
    data2 = json.loads(payload2)
    assert n2 == 1
    assert data2["chosen"][0]["id"] == "S1-P1-T1"


def test_open_decision_holds_a_waiting_review_task(
    env: tuple[NodeRepository, RuntimeRepository, Operations, RecommendationEngine],
) -> None:
    node_repo, runtime_repo, ops, heuristics = env
    plan = _spec_plan(node_repo)
    node_repo.save_node(
        Node(id="DEC-1", kind=NodeKind.DECISION, title="Pick X", status=NodeStatus.NOT_STARTED)
    )
    _task(node_repo, plan, "S1-P1-T1", status=NodeStatus.WAITING_REVIEW, priority=90)
    node_repo.add_relation(
        NodeRelation(source_id="S1-P1-T1", target_id="DEC-1", relation_type=RelationType.DEPENDS_ON)
    )

    payload, n = discover_batch(node_repo, runtime_repo, ops, heuristics, ["S1"], "sess-1", 5, 5)
    data = json.loads(payload)

    assert n == 0
    assert any(
        h.startswith("S1-P1-T1:") and "awaiting owner decision DEC-1" in h for h in data["held"]
    )


def test_excluded_task_is_never_chosen(
    env: tuple[NodeRepository, RuntimeRepository, Operations, RecommendationEngine],
) -> None:
    node_repo, runtime_repo, ops, heuristics = env
    plan = _spec_plan(node_repo)
    _task(node_repo, plan, "S1-P1-T1", priority=90)

    payload, n = discover_batch(
        node_repo, runtime_repo, ops, heuristics, ["S1"], "sess-1", 5, 5, exclude=["S1-P1-T1"]
    )
    data = json.loads(payload)

    assert n == 0
    assert data["held"] == ["S1-P1-T1: excluded by args"]


def test_check_line_checksum_matches_the_payload_byte_for_byte(
    env: tuple[NodeRepository, RuntimeRepository, Operations, RecommendationEngine],
) -> None:
    node_repo, runtime_repo, ops, heuristics = env
    plan = _spec_plan(node_repo)
    _task(node_repo, plan, "S1-P1-T1", priority=90)

    payload, n = discover_batch(node_repo, runtime_repo, ops, heuristics, ["S1"], "sess-1", 5, 5)

    assert n == 1
    # A transcription with one byte flipped must not collide with the real checksum.
    assert djb2(payload) != djb2(payload[:-1] + ("0" if payload[-1] != "0" else "1"))
    recomputed = djb2(payload)
    assert recomputed == djb2(
        json.dumps(json.loads(payload), separators=(",", ":"), sort_keys=True)
    )
