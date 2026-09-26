"""Unit tests for `engine.selection`: the pure rules `Claims`, `tm wave discover` and the wave
simulator all read off a `Snapshot` alone."""

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from taskmanager.core.enums import NodeKind, RelationType
from taskmanager.core.lifecycle import LifecycleError
from taskmanager.core.models import FileLock, Job, Lease, Node, NodeRelation
from taskmanager.core.status import (
    Action,
    DecisionStatus,
    JobKind,
    JobState,
    Outcome,
    Status,
)
from taskmanager.db.connection import DatabaseManager
from taskmanager.db.job_repo import JobRepository
from taskmanager.db.node_repo import NodeRepository
from taskmanager.db.runtime_repo import RuntimeRepository
from taskmanager.engine import selection
from taskmanager.engine.selection import Candidate, Selection
from taskmanager.engine.snapshot import SnapshotBuilder
from taskmanager.engine.stepgraph import Snapshot

MIGRATION = ["api/migrations/versions/001_add.py"]


class Estate:
    """A `state.db` with nothing else around it: no git, no worktrees -- just enough to build a
    real `Snapshot` and inspect its raw sqlite traffic."""

    def __init__(self, root: Path) -> None:
        self.db = DatabaseManager(root / ".taskmanager")
        self.db.init_all()
        self.nodes = NodeRepository(self.db)
        self.runtime = RuntimeRepository(self.db)
        self.jobs = JobRepository(self.db)
        self.builder = SnapshotBuilder(self.nodes, self.runtime, self.jobs)

    def add(
        self,
        node_id: str,
        kind: NodeKind = NodeKind.TASK,
        parent: str | None = None,
        depends: tuple[str, ...] = (),
        **fields: object,
    ) -> Node:
        fields.setdefault("target_repo", None if kind in (NodeKind.SPEC, NodeKind.PLAN) else "api")
        node = Node.model_validate({"id": node_id, "kind": kind, "title": node_id, **fields})
        self.nodes.save_node(node)
        if parent is not None:
            self.nodes.add_relation(
                NodeRelation(
                    source_id=parent, target_id=node_id, relation_type=RelationType.CONTAINS
                )
            )
        for dep in depends:
            self.nodes.add_relation(
                NodeRelation(
                    source_id=node_id, target_id=dep, relation_type=RelationType.DEPENDS_ON
                )
            )
        return node

    def lease(
        self, node_id: str, files: tuple[str, ...] = (), age: timedelta = timedelta(0)
    ) -> None:
        self.runtime.acquire_lease(
            Lease(
                task_id=node_id,
                agent_id=f"agent-{node_id}",
                session_id="s",
                branch_name=f"tm/{node_id}",
                ttl_seconds=60,
                last_heartbeat=datetime.now(tz=UTC) - age,
            ),
            [FileLock(file_path=f, task_id=node_id) for f in files],
        )

    def job(self, node_id: str, kind: JobKind, state: JobState, repo: str = "api") -> str:
        return self.jobs.create(
            Job(kind=kind, node_id=node_id, repo=repo, target="main", state=state)
        ).id

    def node(self, node_id: str) -> Node:
        found = self.nodes.get_node(node_id)
        assert found is not None
        return found

    def snap(self) -> Snapshot:
        return self.builder.build()


@pytest.fixture
def estate(tmp_path: Path) -> Estate:
    return Estate(tmp_path)


@dataclass
class Wave:
    chosen: list[dict[str, object]] = field(default_factory=list)
    held: list[str] = field(default_factory=list)
    waiting_for_slot: int = 0


def wave(
    estate: Estate,
    specs: list[str] | None = None,
    size: int = 10,
    max_strong: int = 10,
    exclude: tuple[str, ...] = (),
    hold_merge: tuple[str, ...] = (),
) -> Wave:
    snap = estate.snap()
    found, held = selection.candidates(snap, specs)
    result = selection.select(found, snap, size, max_strong, exclude, hold_merge)
    return Wave([*result.chosen], [*held, *result.held], result.waiting_for_slot)


def chosen(w: Wave) -> list[tuple[object, object]]:
    return [(entry["id"], entry["action"]) for entry in w.chosen]


# -- next_step and candidates ------------------------------------------------------------------


def test_next_step_of_a_ready_task_is_implement(estate: Estate) -> None:
    node = estate.add("T")
    assert selection.next_step(node) == (Action.IMPLEMENT, "sonnet")


def test_next_step_of_a_completed_node_is_nothing(estate: Estate) -> None:
    node = estate.add("T", status=Status.COMPLETED)
    assert selection.next_step(node) == (None, None)


def test_a_node_the_lifecycle_cannot_read_is_held_and_its_siblings_are_still_chosen(
    estate: Estate,
) -> None:
    """A shape this corrupt never survives `Node`'s own validation, so nothing stored can
    reproduce it; `candidates`' injected `next_step` (what `Claims.next_step`, patched or not,
    plugs in for `discover`) is the only way to raise it here."""
    estate.add("BAD")
    estate.add("OK")

    def unreadable(node: Node) -> tuple[Action | None, str | None]:
        if node.id == "BAD":
            raise LifecycleError("REVIEWED with no outcome")
        return selection.next_step(node)

    found, held = selection.candidates(estate.snap(), None, next_step=unreadable)
    result = selection.select(found, estate.snap(), 10, 10)
    assert [(e["id"], e["action"]) for e in result.chosen] == [("OK", "implement")]
    assert held == ["BAD: REVIEWED with no outcome"]


# -- blocked_reason, everything but a condition ------------------------------------------------


def test_blocked_reason_of_a_completed_node_is_that_it_has_no_next_action(estate: Estate) -> None:
    node = estate.add("T", status=Status.COMPLETED)
    assert selection.blocked_reason(node, estate.snap(), None) == "COMPLETED has no next action"


def test_blocked_reason_waits_on_an_unsatisfied_edge(estate: Estate) -> None:
    estate.add("D")
    node = estate.add("T", depends=("D",))
    snap = estate.snap()
    assert selection.blocked_reason(node, snap, Action.IMPLEMENT) == "waits on D"


def test_blocked_reason_waits_on_an_open_decision(estate: Estate) -> None:
    estate.add("Q", NodeKind.DECISION, status=DecisionStatus.OPEN)
    node = estate.add("T", depends=("Q",))
    snap = estate.snap()
    reason = selection.blocked_reason(node, snap, Action.IMPLEMENT)
    assert reason == "awaiting decision Q"


def test_blocked_reason_sees_a_lease_another_session_holds(estate: Estate) -> None:
    node = estate.add("T")
    estate.lease("T")
    reason = selection.blocked_reason(node, estate.snap(), Action.IMPLEMENT)
    assert reason == f"held by agent-{node.id}"


def test_blocked_reason_sees_a_live_landing_job(estate: Estate) -> None:
    node = estate.add("T", status=Status.MERGING, claimed_from=Status.REVIEWED)
    job_id = estate.job("T", JobKind.LAND, JobState.RUNNING)
    reason = selection.blocked_reason(node, estate.snap(), Action.MERGE)
    assert reason == f"landing job {job_id} is running"


def test_blocked_reason_needs_a_target_repo_to_implement(estate: Estate) -> None:
    node = estate.add("T", target_repo=None)
    assert selection.blocked_reason(node, estate.snap(), Action.IMPLEMENT) == (
        "no target_repo: a task is cut and landed in its target repository"
    )


def test_blocked_reason_sees_declared_files_another_lease_locks(estate: Estate) -> None:
    node = estate.add("T", frontmatter={"declared_files": ["api/app.py"]})
    estate.lease("OTHER", files=("api:api/app.py",))
    reason = selection.blocked_reason(node, estate.snap(), Action.IMPLEMENT)
    assert reason is not None and "declared files locked" in reason


# -- select: migration chain, file overlap, strong slots, exclude, hold_merge -------------------


def test_a_migration_writer_holds_its_repository_chain_until_it_lands_on_main(
    estate: Estate,
) -> None:
    estate.add("A", status=Status.IMPLEMENTED, frontmatter={"declared_files": MIGRATION})
    estate.add("B", frontmatter={"declared_files": ["api/migrations/versions/002_more.py"]})
    estate.add("C", frontmatter={"declared_files": ["api/app.py"]})
    w = wave(estate)
    assert chosen(w) == [("A", "review"), ("C", "implement")]
    assert "B: api migration chain held by A" in w.held


def test_slots_strong_slots_exclusions_and_file_overlap_shape_the_wave(estate: Estate) -> None:
    estate.add("T1", frontmatter={"declared_files": ["api/a.py"]}, priority=90)
    estate.add("T2", frontmatter={"declared_files": ["api/a.py"]}, priority=80)
    estate.add("T3", acceptable_models=["claude-opus-4"], priority=70)
    estate.add("T4", priority=60)
    estate.add("T5", priority=50)
    estate.add("T6", priority=40)

    w = wave(estate, size=2, max_strong=0, exclude=("T4",))

    assert chosen(w) == [("T1", "implement"), ("T5", "implement")]
    assert "T2: declared_files overlap a node chosen this wave" in w.held
    assert "T3: no free opus/fable slot" in w.held
    assert "T4: excluded by args" in w.held
    assert w.waiting_for_slot == 1


def test_a_held_merge_is_skipped_while_the_same_nodes_other_steps_are_offered(
    estate: Estate,
) -> None:
    estate.add("T1", status=Status.REVIEWED, outcome=Outcome.APPROVE)
    estate.add("T2", status=Status.IMPLEMENTED)

    w = wave(estate, hold_merge=("T1", "T2"))

    assert chosen(w) == [("T2", "review")]
    assert "T1: merge held by the dispatcher" in w.held


# -- no SQL statement -----------------------------------------------------------------------------


def test_candidates_and_select_issue_no_sql_statement(estate: Estate) -> None:
    estate.add("A", status=Status.IMPLEMENTED, frontmatter={"declared_files": MIGRATION})
    estate.add("B", frontmatter={"declared_files": ["api/migrations/versions/002_more.py"]})
    estate.add("C", frontmatter={"declared_files": ["api/app.py"]})
    snap = estate.snap()

    calls: list[str] = []
    with estate.db.get_state_connection() as conn:
        conn.set_trace_callback(calls.append)
    try:
        found, held = selection.candidates(snap, None)
        selection.select(found, snap, 10, 10)
    finally:
        with estate.db.get_state_connection() as conn:
            conn.set_trace_callback(None)

    assert calls == []
    assert held == []


def test_select_type_is_the_advertised_one(estate: Estate) -> None:
    result = selection.select([], estate.snap(), 1, 1)
    assert isinstance(result, Selection)
    assert result == Selection(chosen=[], held=[], waiting_for_slot=0)


def test_candidate_carries_the_node_its_action_model_job_and_repos() -> None:
    node = Node(id="T", kind=NodeKind.TASK, title="T")
    cand = Candidate(node, Action.IMPLEMENT, "sonnet", None, ["api"])
    assert (cand.node, cand.action, cand.model, cand.job, cand.repos) == (
        node,
        Action.IMPLEMENT,
        "sonnet",
        None,
        ["api"],
    )
