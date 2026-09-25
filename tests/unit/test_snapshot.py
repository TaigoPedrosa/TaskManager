from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from taskmanager.core.display import Facts
from taskmanager.core.enums import NodeKind, RelationType, VerificationType
from taskmanager.core.lifecycle import Cycle
from taskmanager.core.models import FileLock, Job, Lease, Node, NodeRelation, NodeVerification
from taskmanager.core.status import DecisionStatus, JobKind, JobState, Merge, Outcome, Status
from taskmanager.db.connection import DatabaseManager
from taskmanager.db.job_repo import JobRepository
from taskmanager.db.node_repo import NodeRepository
from taskmanager.db.runtime_repo import RuntimeRepository
from taskmanager.engine.snapshot import SnapshotBuilder, apply_cycle, cycle_of


class Estate:
    def __init__(self, root: Path) -> None:
        db = DatabaseManager(root / ".taskmanager")
        db.init_all()
        self.nodes = NodeRepository(db)
        self.runtime = RuntimeRepository(db)
        self.jobs = JobRepository(db)
        self.builder = SnapshotBuilder(self.nodes, self.runtime, self.jobs)

    def add(
        self,
        node_id: str,
        kind: NodeKind = NodeKind.TASK,
        parent: str | None = None,
        **fields: object,
    ) -> None:
        self.nodes.save_node(
            Node.model_validate({"id": node_id, "kind": kind, "title": node_id, **fields})
        )
        if parent is not None:
            self.nodes.add_relation(
                NodeRelation(
                    source_id=parent, target_id=node_id, relation_type=RelationType.CONTAINS
                )
            )

    def depend(self, dependent: str, dependency: str) -> None:
        self.nodes.add_relation(
            NodeRelation(
                source_id=dependent, target_id=dependency, relation_type=RelationType.DEPENDS_ON
            )
        )

    def lease(
        self, node_id: str, age: timedelta = timedelta(0), files: tuple[str, ...] = ()
    ) -> None:
        self.runtime.acquire_lease(
            Lease(
                task_id=node_id,
                agent_id="a",
                session_id="s",
                branch_name=f"tm/{node_id}",
                ttl_seconds=60,
                last_heartbeat=datetime.now(tz=UTC) - age,
            ),
            [FileLock(file_path=f, task_id=node_id) for f in files],
        )

    def job(self, node_id: str, kind: JobKind, state: JobState) -> None:
        self.jobs.create(Job(kind=kind, node_id=node_id, repo="core", target="main", state=state))

    def facts(self, node_id: str) -> Facts:
        return self.builder.facts(node_id, self.builder.build())


@pytest.fixture
def estate(tmp_path: Path) -> Estate:
    return Estate(tmp_path)


def test_a_snapshot_carries_every_node_its_parent_and_every_edge(estate: Estate) -> None:
    estate.add("S", NodeKind.SPEC, status=Status.READY)
    estate.add("P", NodeKind.PLAN, parent="S", status=Status.READY, review=True, fix=True)
    estate.add(
        "T1",
        parent="P",
        status=Status.IMPLEMENTED,
        merge=Merge.PARENT,
        fix=False,
        target_repo="core",
    )
    estate.add("T2", parent="P", status=Status.READY)
    estate.depend("T2", "T1")
    snap = estate.builder.build()
    assert set(snap.nodes) == {"S", "P", "T1", "T2"}
    assert snap.edges == [("T2", "T1")]
    t1 = snap.nodes["T1"]
    assert (t1.parent, t1.kind, t1.merge, t1.status, t1.review, t1.fix, t1.repo) == (
        "P",
        NodeKind.TASK,
        Merge.PARENT,
        Status.IMPLEMENTED,
        True,
        False,
        "core",
    )
    assert (snap.nodes["P"].review, snap.nodes["S"].review, snap.nodes["S"].parent) == (
        True,
        False,
        None,
    )


@pytest.mark.parametrize(
    ("kind", "stored", "read"),
    [
        (NodeKind.TASK, Status.FAILED, Status.FAILED),
        (NodeKind.DECISION, DecisionStatus.OPEN, DecisionStatus.OPEN),
    ],
)
def test_a_snapshot_reads_every_stored_status_in_the_new_vocabulary(
    estate: Estate, kind: NodeKind, stored: str, read: str
) -> None:
    estate.add("N", kind, status=stored)
    assert estate.builder.build().nodes["N"].status == read


@pytest.mark.parametrize(
    ("files", "migrates"),
    [
        (["core/src/app/models.py"], False),
        (["core/migrations/versions/0007_add_x.py"], True),
    ],
)
def test_a_node_declaring_a_migration_file_writes_a_migration(
    estate: Estate, files: list[str], migrates: bool
) -> None:
    estate.add("T", frontmatter={"declared_files": files})
    assert estate.builder.build().nodes["T"].writes_migration is migrates


@pytest.mark.parametrize(
    ("command", "literal"),
    [
        ("git -C core show origin/main:app.py | grep -q x", True),
        ('git -C core show "$TM_VERIFY_REF":app.py | grep -q x', False),
        ('git -C core show "${TM_VERIFY_REF:-origin/main}":app.py | grep -q x', False),
        (
            'git -C core show "${TM_VERIFY_REF:-origin/main}":a.py && git -C core log origin/main',
            True,
        ),
        ('git -C core show "${OTHER_REF:-origin/main}":app.py | grep -q x', True),
    ],
    ids=["literal", "variable", "variable-defaulting-to-it", "default-and-literal", "other-var"],
)
def test_a_test_command_naming_origin_main_is_flagged(
    estate: Estate, command: str, literal: bool
) -> None:
    estate.add("T")
    estate.nodes.add_verification(
        NodeVerification(
            node_id="T",
            verification_type=VerificationType.TEST_COMMAND,
            target_path="check",
            expected_pattern=command,
        )
    )
    assert estate.builder.build().nodes["T"].literal_origin_main is literal


@pytest.mark.parametrize(
    ("arrange", "busy"),
    [
        (lambda e: None, False),
        (lambda e: e.lease("T"), True),
        (lambda e: e.lease("T", age=timedelta(hours=1)), False),
        (lambda e: e.job("T", JobKind.LAND, JobState.RUNNING), True),
        (lambda e: e.job("T", JobKind.LAND, JobState.NEEDS_AGENT), True),
        (lambda e: e.job("T", JobKind.LAND, JobState.SUCCEEDED), False),
        (lambda e: e.job("T", JobKind.SYNC, JobState.OWN_DEFECT), False),
    ],
    ids=[
        "idle",
        "live-lease",
        "expired-lease",
        "running-job",
        "job-waiting-for-agent",
        "finished-job",
        "failed-job",
    ],
)
def test_a_node_is_busy_while_it_holds_a_live_lease_or_an_unfinished_job(
    estate: Estate, arrange: Callable[[Estate], object], busy: bool
) -> None:
    estate.add("T")
    arrange(estate)
    assert estate.builder.build().nodes["T"].busy is busy


def test_a_cycle_carries_the_stored_lifecycle_fields() -> None:
    node = Node(
        id="P",
        kind=NodeKind.PLAN,
        title="p",
        status=Status.REVIEWED,
        review=True,
        fix=True,
        outcome=Outcome.REJECT,
        fix_for=Outcome.REJECT,
        claimed_from=None,
        review_cycles=2,
        merge_attempts=1,
        step_failures=1,
    )
    assert cycle_of(node) == Cycle(
        status=Status.REVIEWED,
        container=True,
        review=True,
        fix=True,
        outcome=Outcome.REJECT,
        fix_for=Outcome.REJECT,
        review_cycles=2,
        merge_attempts=1,
        step_failures=1,
    )


def test_applying_a_cycle_round_trips_through_storage(estate: Estate) -> None:
    estate.add("T", status=Status.READY)
    node = estate.nodes.get_node("T")
    assert node is not None
    moved = Cycle(status=Status.IMPLEMENTING, claimed_from=Status.READY, step_failures=2)
    estate.nodes.save_node(apply_cycle(node, moved))
    stored = estate.nodes.get_node("T")
    assert stored is not None and cycle_of(stored) == moved


def test_facts_of_an_idle_ready_task_are_all_clear(estate: Estate) -> None:
    estate.add("T", status=Status.READY)
    assert estate.facts("T") == Facts()


@pytest.mark.parametrize(
    ("age", "lease"), [(timedelta(0), "live"), (timedelta(hours=1), "expired")]
)
def test_facts_tell_a_live_lease_from_an_expired_one(
    estate: Estate, age: timedelta, lease: str
) -> None:
    estate.add("T", status=Status.IMPLEMENTING, claimed_from=Status.READY)
    estate.lease("T", age=age)
    assert estate.facts("T").lease == lease


def test_facts_see_a_landing_job_waiting_for_an_agent(estate: Estate) -> None:
    estate.add("T", status=Status.MERGING, claimed_from=Status.REVIEWED)
    estate.job("T", JobKind.LAND, JobState.NEEDS_AGENT)
    assert estate.facts("T").job_needs_agent is True


@pytest.mark.parametrize(
    ("decision_status", "open_decision"),
    [
        (DecisionStatus.OPEN, True),
        (DecisionStatus.ANSWERED, False),
        (DecisionStatus.WITHDRAWN, False),
    ],
)
def test_facts_see_an_open_decision_on_the_node_or_any_ancestor(
    estate: Estate, decision_status: DecisionStatus, open_decision: bool
) -> None:
    estate.add("P", NodeKind.PLAN, status=Status.READY)
    estate.add("T", parent="P", status=Status.READY)
    estate.add("D1", NodeKind.DECISION, status=decision_status)
    estate.depend("P", "D1")
    assert estate.facts("T").open_decision is open_decision
    assert estate.facts("T").unsatisfied_edge is False


@pytest.mark.parametrize(
    ("dependency_status", "unsatisfied"),
    [
        (Status.READY, True),
        (Status.MERGING, True),
        (Status.FAILED, True),
        (Status.COMPLETED, False),
        (Status.SUPERSEDED, False),
    ],
)
def test_facts_see_an_unsatisfied_edge_own_or_inherited(
    estate: Estate, dependency_status: Status, unsatisfied: bool
) -> None:
    estate.add("P", NodeKind.PLAN, status=Status.READY)
    estate.add("T", parent="P", status=Status.READY)
    claimed_from = Status.IMPLEMENTED if dependency_status == Status.MERGING else None
    estate.add("Y", status=dependency_status, claimed_from=claimed_from)
    estate.add("Z", status=dependency_status, claimed_from=claimed_from)
    estate.depend("T", "Y")
    estate.depend("P", "Z")
    assert estate.facts("T").unsatisfied_edge is unsatisfied
    assert estate.facts("P").unsatisfied_edge is unsatisfied


@pytest.mark.parametrize(
    ("state", "pending"),
    [
        (JobState.RUNNING, True),
        (JobState.NEEDS_AGENT, True),
        (JobState.SUCCEEDED, False),
    ],
)
def test_facts_see_a_sync_the_claim_waits_on(
    estate: Estate, state: JobState, pending: bool
) -> None:
    estate.add("T", status=Status.READY)
    estate.job("T", JobKind.SYNC, state)
    assert estate.facts("T").sync_pending is pending


@pytest.mark.parametrize(
    ("status", "locked"),
    [
        (Status.READY, True),
        (Status.REVIEWED, True),
        (Status.IMPLEMENTED, False),
        (Status.FIXED, False),
    ],
)
def test_facts_see_a_declared_file_locked_only_when_the_next_action_locks_files(
    estate: Estate, status: Status, locked: bool
) -> None:
    estate.add(
        "T",
        status=status,
        outcome=Outcome.REJECT if status in (Status.REVIEWED, Status.FIXED) else None,
        fix_for=Outcome.REJECT if status == Status.FIXED else None,
        frontmatter={"declared_files": ["a.py"]},
    )
    estate.add("OTHER", status=Status.IMPLEMENTING, claimed_from=Status.READY)
    estate.lease("OTHER", files=("a.py",))
    assert estate.facts("T").files_locked is locked


def test_a_container_declaring_no_files_locks_its_descendants_files(estate: Estate) -> None:
    estate.add("P", NodeKind.PLAN, status=Status.READY)
    estate.add("T1", parent="P", frontmatter={"declared_files": ["a.py", "b.py"]})
    estate.add("T2", parent="P", frontmatter={"declared_files": ["b.py", "c.py"]})
    snap = estate.builder.build()
    assert estate.builder.lock_set("P", snap) == ["a.py", "b.py", "c.py"]
    estate.add("Q", NodeKind.PLAN, status=Status.READY, frontmatter={"declared_files": ["q.py"]})
    assert estate.builder.lock_set("Q", estate.builder.build()) == ["q.py"]


@pytest.mark.parametrize(
    ("child_statuses", "started"),
    [
        ([Status.READY, Status.READY], False),
        ([Status.READY, Status.ABANDONED], False),
        ([Status.READY, Status.IMPLEMENTING], True),
        ([Status.COMPLETED, Status.READY], True),
    ],
)
def test_facts_see_a_container_whose_descendant_has_started(
    estate: Estate, child_statuses: list[Status], started: bool
) -> None:
    estate.add("S", NodeKind.SPEC, status=Status.READY)
    estate.add("P", NodeKind.PLAN, parent="S", status=Status.READY)
    for n, status in enumerate(child_statuses):
        claimed_from = Status.READY if status == Status.IMPLEMENTING else None
        estate.add(f"T{n}", parent="P", status=status, claimed_from=claimed_from)
    assert estate.facts("S").descendant_started is started


def test_a_writer_held_only_by_its_repositorys_migration_chain_shows_blocked_by_task(
    estate: Estate,
) -> None:
    migration = {"declared_files": ["core/migrations/versions/001.py"]}
    estate.add(
        "A",
        status=Status.IMPLEMENTING,
        claimed_from=Status.READY,
        target_repo="core",
        frontmatter=migration,
    )
    estate.add("B", target_repo="core", frontmatter=migration)
    estate.add("C", target_repo="core")
    assert estate.facts("B").unsatisfied_edge is True
    assert estate.facts("C").unsatisfied_edge is False
