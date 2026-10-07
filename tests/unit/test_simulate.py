"""Unit tests for `engine.simulate`: `depth` waves out from a live snapshot, each node's step
assumed to succeed."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from taskmanager.core.enums import NodeKind, RelationType
from taskmanager.core.models import FileLock, Lease, Node, NodeRelation
from taskmanager.core.status import Action, DecisionStatus, Status
from taskmanager.db.connection import DatabaseManager
from taskmanager.db.job_repo import JobRepository
from taskmanager.db.node_repo import NodeRepository
from taskmanager.db.runtime_repo import RuntimeRepository
from taskmanager.engine import selection
from taskmanager.engine.simulate import Wave, WaveEntry, simulate
from taskmanager.engine.snapshot import SnapshotBuilder
from taskmanager.engine.stepgraph import Snapshot


class Estate:
    """A `state.db` with nothing else around it: enough to build a real `Snapshot` and check
    what it, and the SQL traffic that built it, look like -- `simulate` itself must add none."""

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

    def snap(self) -> Snapshot:
        return self.builder.build()


@pytest.fixture
def estate(tmp_path: Path) -> Estate:
    return Estate(tmp_path)


def action_of(wave: Wave, node_id: str) -> WaveEntry:
    return next(e for e in wave.entries if e.id == node_id)


# -- wave 1: candidates + select, plus in flight -------------------------------------------------


def test_wave_one_matches_candidates_and_select_on_the_live_snapshot(estate: Estate) -> None:
    estate.add("T1", priority=90)
    estate.add("T2", priority=10)
    snap = estate.snap()
    found, held = selection.candidates(snap, None)
    expected = selection.select(found, snap, 10, 10)

    waves = simulate(snap, depth=1, size=10, max_strong=10, specs=None)

    assert [(e.id, e.action) for e in waves[0].entries] == [
        (c["id"], c["action"]) for c in expected.chosen
    ]
    assert waves[0].held == [*held, *expected.held]


def test_a_node_already_mid_step_is_wave_one_marked_in_flight(estate: Estate) -> None:
    estate.add("T1", status=Status.IMPLEMENTING, claimed_from=Status.READY)
    estate.add("T2")

    waves = simulate(estate.snap(), depth=1, size=10, max_strong=10, specs=None)

    entry = action_of(waves[0], "T1")
    assert (entry.in_flight, entry.action, entry.status_before, entry.status_after) == (
        True,
        Action.IMPLEMENT,
        Status.IMPLEMENTING,
        Status.IMPLEMENTED,
    )
    assert action_of(waves[0], "T2").in_flight is False


def test_an_in_flight_containers_repos_follow_repo_order(estate: Estate) -> None:
    estate.add("P", NodeKind.PLAN, status=Status.REVIEWING, claimed_from=Status.IMPLEMENTED)
    estate.add("A", parent="P", target_repo="alpha", status=Status.COMPLETED)
    estate.add("Z", parent="P", target_repo="zeta", status=Status.COMPLETED)

    waves = simulate(
        estate.snap(), depth=1, size=10, max_strong=10, specs=None, repo_order=["zeta", "alpha"]
    )

    assert action_of(waves[0], "P").repos == ["zeta", "alpha"]


# -- a task marches implement -> review -> merge, one wave per step ------------------------------


def test_a_reviewed_task_appears_as_implement_then_review_then_merge(estate: Estate) -> None:
    estate.add("T1")

    waves = simulate(estate.snap(), depth=3, size=10, max_strong=10, specs=None)

    assert [action_of(w, "T1").action for w in waves] == [
        Action.IMPLEMENT,
        Action.REVIEW,
        Action.MERGE,
    ]
    assert action_of(waves[0], "T1").status_after == Status.IMPLEMENTED
    assert action_of(waves[1], "T1").status_after == Status.REVIEWED
    assert action_of(waves[2], "T1").status_after == Status.COMPLETED


def test_a_task_with_review_off_skips_straight_from_implement_to_merge(estate: Estate) -> None:
    estate.add("T1", review=False, fix=False)

    waves = simulate(estate.snap(), depth=2, size=10, max_strong=10, specs=None)

    assert [action_of(w, "T1").action for w in waves] == [Action.IMPLEMENT, Action.MERGE]


# -- a plan's rollup and its own review, exactly as core.rollup decides --------------------------


def test_a_task_under_a_plan_lands_on_the_plan_and_the_plan_lands_before_its_one_review(
    estate: Estate,
) -> None:
    estate.add("P", NodeKind.PLAN, review=True, fix=True)
    estate.add("T1", parent="P")

    waves = simulate(estate.snap(), depth=5, size=10, max_strong=10, specs=None)

    assert [action_of(w, "T1").action for w in waves[:3]] == [
        Action.IMPLEMENT,
        Action.REVIEW,
        Action.MERGE,
    ]
    assert action_of(waves[2], "T1").status_after == Status.COMPLETED
    # The plan rolls up to IMPLEMENTED the moment its only child lands, in that same wave.
    assert (action_of(waves[3], "P").action, action_of(waves[3], "P").status_after) == (
        Action.MERGE,
        Status.LANDED,
    )
    assert (action_of(waves[4], "P").action, action_of(waves[4], "P").status_after) == (
        Action.REVIEW,
        Status.COMPLETED,
    )


# -- a dependent waits for its dependency's merge -------------------------------------------------


def test_a_dependent_appears_the_wave_after_its_dependency_merges(estate: Estate) -> None:
    estate.add("D")
    estate.add("T1", depends=("D",))

    waves = simulate(estate.snap(), depth=4, size=10, max_strong=10, specs=None)

    assert waves[0].held == ["T1: waits on D"]
    assert [e.id for w in waves[:3] for e in w.entries if e.id == "D"] == ["D", "D", "D"]
    assert action_of(waves[2], "D").status_after == Status.COMPLETED
    assert action_of(waves[3], "T1").action == Action.IMPLEMENT


# -- an edge onto an open decision holds every wave -----------------------------------------------


def test_an_edge_onto_an_open_decision_holds_its_node_every_wave(estate: Estate) -> None:
    estate.add("Q", NodeKind.DECISION, status=DecisionStatus.OPEN)
    estate.add("T1", depends=("Q",))

    waves = simulate(estate.snap(), depth=3, size=10, max_strong=10, specs=None)

    assert all(w.held == ["T1: awaiting decision Q"] for w in waves)
    assert all(e.id != "T1" for w in waves for e in w.entries)


# -- wave entry shape and no side effects ---------------------------------------------------------


def test_wave_entry_carries_id_title_kind_action_model_repos_and_status(estate: Estate) -> None:
    estate.add("T1", priority=77)

    waves = simulate(estate.snap(), depth=1, size=10, max_strong=10, specs=None)
    entry = action_of(waves[0], "T1")

    assert (entry.id, entry.title, entry.kind, entry.repos) == ("T1", "T1", NodeKind.TASK, ["api"])
    assert (entry.action, entry.model) == (Action.IMPLEMENT, "sonnet")
    assert (entry.status_before, entry.status_after) == (Status.READY, Status.IMPLEMENTED)


def test_the_input_snapshot_is_never_mutated(estate: Estate) -> None:
    estate.add("T1")
    snap = estate.snap()
    before = {n: s.status for n, s in snap.nodes.items()}

    simulate(snap, depth=3, size=10, max_strong=10, specs=None)

    assert {n: s.status for n, s in snap.nodes.items()} == before
    assert snap.graph_data().nodes["T1"].status == Status.READY


def test_simulate_issues_no_sql_statement(estate: Estate) -> None:
    estate.add("P", NodeKind.PLAN, review=True, fix=True)
    estate.add("T1", parent="P")
    estate.add("T2", depends=("T1",))
    snap = estate.snap()

    calls: list[str] = []
    with estate.db.get_state_connection() as conn:
        conn.set_trace_callback(calls.append)
    try:
        simulate(snap, depth=4, size=10, max_strong=10, specs=None)
    finally:
        with estate.db.get_state_connection() as conn:
            conn.set_trace_callback(None)

    assert calls == []
