import os
import resource
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path

from taskmanager.core.enums import (
    LockType,
    NodeKind,
    RelationType,
    VerificationType,
)
from taskmanager.core.models import (
    FileLock,
    Lease,
    LedgerEvent,
    Node,
    NodeRelation,
    NodeSection,
    NodeVerification,
)
from taskmanager.core.status import Status
from taskmanager.db.connection import DatabaseManager
from taskmanager.db.ledger_repo import LedgerRepository
from taskmanager.db.node_repo import NodeRepository
from taskmanager.db.runtime_repo import RuntimeRepository


def test_spec_connection_is_reused_not_reopened(tmp_path: Path) -> None:
    # A fresh sqlite3 connection per call (extension load and all) dominates load time under
    # hundreds of NodeRepository calls per web request: one connection per DatabaseManager,
    # reused, keeps that cost paid once.
    db = DatabaseManager(tmp_path)
    db.init_all()
    with db.get_state_connection() as first:
        pass
    with db.get_state_connection() as second:
        pass
    assert first is second


def test_connections_of_finished_threads_are_closed(tmp_path: Path) -> None:
    # The web server runs sync routes on anyio worker threads that retire after 10s idle, so a
    # per-thread connection kept alive past its thread's death leaks its file descriptors until
    # the process hits the fd limit and every new thread fails with "unable to open database
    # file". The soft limit is lowered so the leak, not the machine's headroom, decides.
    db = DatabaseManager(tmp_path)
    db.init_all()

    failures: list[sqlite3.Error] = []

    def query() -> None:
        try:
            with db.get_state_connection() as conn:
                conn.execute("SELECT COUNT(*) FROM leases").fetchone()
        except sqlite3.Error as exc:
            failures.append(exc)

    open_fds = len(os.listdir("/dev/fd"))
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    resource.setrlimit(resource.RLIMIT_NOFILE, (open_fds + 20, hard))
    try:
        for _ in range(40):
            worker = threading.Thread(target=query)
            worker.start()
            worker.join()
    finally:
        resource.setrlimit(resource.RLIMIT_NOFILE, (soft, hard))
    assert failures == []


def test_node_repo_crud(tmp_path: Path) -> None:
    db = DatabaseManager(tmp_path)
    db.init_all()
    repo = NodeRepository(db)

    node = Node(
        id="AUTH-T01",
        kind=NodeKind.TASK,
        title="Test Task",
        status=Status.READY,
        priority=60,
        target_repo="backend",
        acceptable_models=["claude-3-7-sonnet"],
        frontmatter={"risk": "medium"},
    )
    repo.save_node(node)

    loaded = repo.get_node("AUTH-T01")
    assert loaded is not None
    assert loaded.id == "AUTH-T01"
    assert loaded.kind == NodeKind.TASK
    assert loaded.title == "Test Task"
    assert loaded.status == Status.READY
    assert loaded.priority == 60
    assert loaded.target_repo == "backend"
    assert loaded.acceptable_models == ["claude-3-7-sonnet"]
    assert loaded.frontmatter == {"risk": "medium"}

    node.title = "Updated Task Title"
    node.status = Status.IMPLEMENTING
    node.claimed_from = Status.READY
    node.priority = 90
    repo.save_node(node)

    updated = repo.get_node("AUTH-T01")
    assert updated is not None
    assert updated.title == "Updated Task Title"
    assert updated.status == Status.IMPLEMENTING
    assert updated.priority == 90

    assert repo.get_node("NONEXISTENT") is None


def test_node_repo_list_nodes(tmp_path: Path) -> None:
    db = DatabaseManager(tmp_path)
    db.init_all()
    repo = NodeRepository(db)

    repo.save_node(Node(id="AUTH-S01", kind=NodeKind.SPEC, title="Spec", status=Status.COMPLETED))
    repo.save_node(Node(id="AUTH-P01", kind=NodeKind.PLAN, title="Plan", status=Status.READY))
    repo.save_node(Node(id="AUTH-T01", kind=NodeKind.TASK, title="Task 1", status=Status.READY))
    repo.save_node(Node(id="AUTH-T02", kind=NodeKind.TASK, title="Task 2", status=Status.COMPLETED))

    all_nodes = repo.list_nodes()
    assert len(all_nodes) == 4

    tasks = repo.list_nodes(kind=NodeKind.TASK)
    assert len(tasks) == 2
    assert {n.id for n in tasks} == {"AUTH-T01", "AUTH-T02"}

    completed = repo.list_nodes(status=Status.COMPLETED)
    assert len(completed) == 2
    assert {n.id for n in completed} == {"AUTH-S01", "AUTH-T02"}

    completed_tasks = repo.list_nodes(kind=NodeKind.TASK, status=Status.COMPLETED)
    assert len(completed_tasks) == 1
    assert completed_tasks[0].id == "AUTH-T02"


def test_node_repo_sections(tmp_path: Path) -> None:
    db = DatabaseManager(tmp_path)
    db.init_all()
    repo = NodeRepository(db)

    node = Node(id="AUTH-T01", kind=NodeKind.TASK, title="Auth Task")
    repo.save_node(node)

    sec2 = NodeSection(
        node_id="AUTH-T01",
        section_key="tests",
        ordinal=2,
        header="## Tests",
        content="Run pytest",
    )
    sec1 = NodeSection(
        node_id="AUTH-T01",
        section_key="steps",
        ordinal=1,
        header="## Steps",
        content="- [ ] Step 1",
    )
    repo.save_section(sec2)
    repo.save_section(sec1)

    loaded_sec = repo.get_section("AUTH-T01", "steps")
    assert loaded_sec is not None
    assert loaded_sec.content == "- [ ] Step 1"
    assert loaded_sec.header == "## Steps"

    assert repo.get_section("AUTH-T01", "nonexistent") is None

    all_secs = repo.get_all_sections("AUTH-T01")
    assert len(all_secs) == 2
    assert [s.section_key for s in all_secs] == ["steps", "tests"]

    sec1_updated = NodeSection(
        node_id="AUTH-T01",
        section_key="steps",
        ordinal=1,
        header="## Steps",
        content="- [x] Step 1 finished",
    )
    repo.save_section(sec1_updated)
    reloaded = repo.get_section("AUTH-T01", "steps")
    assert reloaded is not None
    assert reloaded.content == "- [x] Step 1 finished"

    with db.get_state_connection() as conn:
        row = conn.execute(
            "SELECT node_id FROM nodes_fts WHERE nodes_fts MATCH 'finished'"
        ).fetchone()
        assert row is not None
        assert row[0] == "AUTH-T01"


def test_node_repo_relations(tmp_path: Path) -> None:
    db = DatabaseManager(tmp_path)
    db.init_all()
    repo = NodeRepository(db)

    repo.save_node(Node(id="AUTH-P01", kind=NodeKind.PLAN, title="Plan"))
    repo.save_node(Node(id="AUTH-T01", kind=NodeKind.TASK, title="Task 1"))
    repo.save_node(Node(id="AUTH-T02", kind=NodeKind.TASK, title="Task 2"))

    repo.add_relation(
        NodeRelation(
            source_id="AUTH-P01",
            target_id="AUTH-T01",
            relation_type=RelationType.CONTAINS,
        )
    )
    repo.add_relation(
        NodeRelation(
            source_id="AUTH-P01",
            target_id="AUTH-T02",
            relation_type=RelationType.CONTAINS,
        )
    )

    children = repo.get_children("AUTH-P01")
    assert "AUTH-T01" in children
    assert "AUTH-T02" in children

    repo.add_relation(
        NodeRelation(
            source_id="AUTH-T02",
            target_id="AUTH-T01",
            relation_type=RelationType.DEPENDS_ON,
        )
    )

    deps = repo.get_dependencies("AUTH-T02")
    assert deps == ["AUTH-T01"]

    blocked = repo.get_blocked_by("AUTH-T01")
    assert blocked == ["AUTH-T02"]


def test_node_repo_get_ancestor_of_kind_walks_nested_plans(tmp_path: Path) -> None:
    db = DatabaseManager(tmp_path)
    db.init_all()
    repo = NodeRepository(db)

    repo.save_node(Node(id="SPEC-A", kind=NodeKind.SPEC, title="Spec A"))
    repo.save_node(Node(id="PLAN-OUTER", kind=NodeKind.PLAN, title="Outer Plan"))
    repo.save_node(Node(id="PLAN-INNER", kind=NodeKind.PLAN, title="Inner Plan"))
    repo.save_node(Node(id="TASK-1", kind=NodeKind.TASK, title="Task"))
    repo.add_relation(
        NodeRelation(
            source_id="SPEC-A", target_id="PLAN-OUTER", relation_type=RelationType.CONTAINS
        )
    )
    repo.add_relation(
        NodeRelation(
            source_id="PLAN-OUTER", target_id="PLAN-INNER", relation_type=RelationType.CONTAINS
        )
    )
    repo.add_relation(
        NodeRelation(
            source_id="PLAN-INNER", target_id="TASK-1", relation_type=RelationType.CONTAINS
        )
    )

    assert repo.get_ancestor_of_kind("TASK-1", NodeKind.SPEC) == "SPEC-A"
    assert repo.get_ancestor_of_kind("PLAN-INNER", NodeKind.SPEC) == "SPEC-A"

    # A plan chain that never reaches a spec: genuinely spec-less.
    repo.save_node(Node(id="ORPHAN-PLAN", kind=NodeKind.PLAN, title="Orphan"))
    repo.save_node(Node(id="ORPHAN-TASK", kind=NodeKind.TASK, title="Orphan Task"))
    repo.add_relation(
        NodeRelation(
            source_id="ORPHAN-PLAN", target_id="ORPHAN-TASK", relation_type=RelationType.CONTAINS
        )
    )
    assert repo.get_ancestor_of_kind("ORPHAN-TASK", NodeKind.SPEC) is None

    # A CONTAINS cycle terminates instead of looping forever.
    repo.save_node(Node(id="CYCLE-A", kind=NodeKind.PLAN, title="Cycle A"))
    repo.save_node(Node(id="CYCLE-B", kind=NodeKind.PLAN, title="Cycle B"))
    repo.add_relation(
        NodeRelation(source_id="CYCLE-A", target_id="CYCLE-B", relation_type=RelationType.CONTAINS)
    )
    repo.add_relation(
        NodeRelation(source_id="CYCLE-B", target_id="CYCLE-A", relation_type=RelationType.CONTAINS)
    )
    assert repo.get_ancestor_of_kind("CYCLE-A", NodeKind.SPEC) is None


def test_node_repo_transfer_blocks(tmp_path: Path) -> None:
    db = DatabaseManager(tmp_path)
    db.init_all()
    repo = NodeRepository(db)

    repo.save_node(Node(id="T-OLD", kind=NodeKind.TASK, title="Old Task"))
    repo.save_node(Node(id="T-NEW", kind=NodeKind.TASK, title="New Task"))
    repo.save_node(Node(id="T-A", kind=NodeKind.TASK, title="Task A"))
    repo.save_node(Node(id="T-B", kind=NodeKind.TASK, title="Task B"))
    repo.save_node(Node(id="T-C", kind=NodeKind.TASK, title="Task C"))

    repo.add_relation(
        NodeRelation(source_id="T-A", target_id="T-OLD", relation_type=RelationType.DEPENDS_ON)
    )
    repo.add_relation(
        NodeRelation(source_id="T-B", target_id="T-OLD", relation_type=RelationType.DEPENDS_ON)
    )
    repo.add_relation(
        NodeRelation(source_id="T-C", target_id="T-OLD", relation_type=RelationType.DEPENDS_ON)
    )

    repo.transfer_blocks("T-OLD", "T-NEW", transfer_mode="none")
    assert set(repo.get_blocked_by("T-OLD")) == {"T-A", "T-B", "T-C"}
    assert repo.get_blocked_by("T-NEW") == []

    repo.transfer_blocks("T-OLD", "T-NEW", transfer_mode="custom", custom_ids=["T-A"])
    assert set(repo.get_blocked_by("T-OLD")) == {"T-B", "T-C"}
    assert repo.get_blocked_by("T-NEW") == ["T-A"]

    repo.transfer_blocks("T-OLD", "T-NEW", transfer_mode="all")
    assert repo.get_blocked_by("T-OLD") == []
    assert set(repo.get_blocked_by("T-NEW")) == {"T-A", "T-B", "T-C"}


def test_node_repo_verifications(tmp_path: Path) -> None:
    db = DatabaseManager(tmp_path)
    db.init_all()
    repo = NodeRepository(db)

    repo.save_node(Node(id="AUTH-T01", kind=NodeKind.TASK, title="Auth Task"))

    v1 = NodeVerification(
        node_id="AUTH-T01",
        verification_type=VerificationType.FILE_EXISTS,
        target_path="src/auth/jwt.py",
    )
    v2 = NodeVerification(
        node_id="AUTH-T01",
        verification_type=VerificationType.TEST_COMMAND,
        target_path="tests/unit/test_jwt.py",
        expected_pattern="PASSED",
    )
    repo.add_verification(v1)
    repo.add_verification(v2)

    verifications = repo.get_verifications("AUTH-T01")
    assert len(verifications) == 2
    assert verifications[0].node_id == "AUTH-T01"
    assert verifications[0].verification_type == VerificationType.FILE_EXISTS
    assert verifications[0].target_path == "src/auth/jwt.py"
    assert verifications[1].verification_type == VerificationType.TEST_COMMAND
    assert verifications[1].expected_pattern == "PASSED"

    assert repo.get_verifications("NONEXISTENT") == []


def test_runtime_repo_leases_and_locks(tmp_path: Path) -> None:
    db = DatabaseManager(tmp_path)
    db.init_all()
    repo = RuntimeRepository(db)

    lease = Lease(
        task_id="AUTH-T01",
        agent_id="agent-1",
        session_id="sess-1",
        account_id="acc-prod",
        worktree_path="/tmp/worktree",
        branch_name="tm/AUTH-T01",
        ttl_seconds=300,
    )
    lock1 = FileLock(file_path="src/auth/jwt.py", task_id="AUTH-T01")
    lock2 = FileLock(file_path="src/auth/utils.py", task_id="AUTH-T01", lock_type=LockType.READ)
    repo.acquire_lease(lease, [lock1, lock2])

    loaded = repo.get_lease("AUTH-T01")
    assert loaded is not None
    assert loaded.task_id == "AUTH-T01"
    assert loaded.agent_id == "agent-1"
    assert loaded.session_id == "sess-1"
    assert loaded.account_id == "acc-prod"
    assert loaded.worktree_path == "/tmp/worktree"
    assert loaded.branch_name == "tm/AUTH-T01"
    assert loaded.ttl_seconds == 300

    assert repo.is_file_locked("src/auth/jwt.py")
    assert repo.is_file_locked("src/auth/utils.py")
    assert not repo.is_file_locked("src/auth/other.py")

    conflicts = repo.get_conflicting_tasks(["src/auth/jwt.py", "src/auth/other.py"])
    assert "src/auth/jwt.py" in conflicts
    assert conflicts["src/auth/jwt.py"] == "Task: AUTH-T01, Agent: agent-1"
    assert "src/auth/other.py" not in conflicts

    assert repo.heartbeat("AUTH-T01") is True
    assert repo.heartbeat("NONEXISTENT") is False

    repo.release_lease("AUTH-T01")
    assert repo.get_lease("AUTH-T01") is None
    assert not repo.is_file_locked("src/auth/jwt.py")


def test_runtime_repo_sweep_expired_leases(tmp_path: Path) -> None:
    db = DatabaseManager(tmp_path)
    db.init_all()
    repo = RuntimeRepository(db)

    lease = Lease(
        task_id="AUTH-T01",
        agent_id="agent-1",
        session_id="sess-1",
        branch_name="tm/AUTH-T01",
        ttl_seconds=1,
    )
    lock = FileLock(file_path="src/auth/jwt.py", task_id="AUTH-T01")
    repo.acquire_lease(lease, [lock])

    assert repo.get_lease("AUTH-T01") is not None
    assert repo.is_file_locked("src/auth/jwt.py")

    time.sleep(1.1)
    expired = repo.sweep_expired_leases()
    assert "AUTH-T01" in expired
    assert repo.get_lease("AUTH-T01") is None
    assert not repo.is_file_locked("src/auth/jwt.py")


def test_ledger_repo_append_and_list(tmp_path: Path) -> None:
    db = DatabaseManager(tmp_path)
    db.init_all()
    repo = LedgerRepository(db)

    e1 = LedgerEvent(
        actor_id="user",
        command="task.create",
        target_id="AUTH-T01",
        payload={"title": "Create auth"},
        diff={"created": True},
    )
    e2 = LedgerEvent(
        actor_id="agent-1",
        command="task.start",
        target_id="AUTH-T01",
        payload={"agent": "agent-1"},
        diff={"status": ["NOT_STARTED", "IMPLEMENTING"]},
    )
    e3 = LedgerEvent(
        actor_id="user",
        command="task.create",
        target_id="AUTH-T02",
        payload={"title": "Create user profile"},
        diff={"created": True},
    )

    repo.append(e1)
    repo.append(e2)
    repo.append(e3)

    assert e1.id is not None
    assert e2.id is not None
    assert e3.id is not None

    all_events = repo.list_events(limit=10)
    assert len(all_events) == 3
    assert [e.id for e in all_events] == [e3.id, e2.id, e1.id]

    t1_events = repo.list_events(target_id="AUTH-T01", limit=10)
    assert len(t1_events) == 2
    assert [e.id for e in t1_events] == [e2.id, e1.id]
    assert t1_events[0].command == "task.start"
    assert t1_events[0].payload == {"agent": "agent-1"}
    assert t1_events[0].diff == {"status": ["NOT_STARTED", "IMPLEMENTING"]}
    assert isinstance(t1_events[0].timestamp, datetime)


def test_concurrent_threads_never_share_a_connection(tmp_path: Path) -> None:
    # The web server runs its database routes in a threadpool against one DatabaseManager; a
    # single shared sqlite3 connection interleaves their cursors and a fetchone() returns
    # another request's row or None.
    import threading

    db = DatabaseManager(tmp_path)
    db.init_all()
    repo = NodeRepository(db)
    for i in range(20):
        repo.save_node(Node(id=f"T{i}", kind=NodeKind.TASK, title=f"t{i}"))
    errors: list[BaseException] = []
    start = threading.Barrier(8)

    def reader() -> None:
        start.wait()
        try:
            for _ in range(150):
                for i in range(20):
                    node = repo.get_node(f"T{i}")
                    assert node is not None and node.id == f"T{i}"
        except BaseException as exc:  # noqa: BLE001 -- collected and re-raised below
            errors.append(exc)

    threads = [threading.Thread(target=reader) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []


def test_a_transaction_on_one_thread_does_not_swallow_another_threads_commit(
    tmp_path: Path,
) -> None:
    # A write on another thread must never join this thread's open transaction: when this
    # one rolls back, only its own write goes, and the other thread's write still lands (after
    # waiting for sqlite's single-writer lock).
    import threading

    db = DatabaseManager(tmp_path)
    db.init_all()
    repo = NodeRepository(db)
    inside = threading.Event()

    def writer() -> None:
        inside.wait(5)
        repo.save_node(Node(id="OUTSIDE", kind=NodeKind.TASK, title="outside"))

    other = threading.Thread(target=writer)
    other.start()
    try:
        with repo.transaction():
            repo.save_node(Node(id="IN-TX", kind=NodeKind.TASK, title="in tx"))
            inside.set()
            time.sleep(0.3)
            raise RuntimeError("roll this transaction back")
    except RuntimeError:
        pass
    other.join(10)
    fresh = NodeRepository(DatabaseManager(tmp_path))
    assert fresh.get_node("IN-TX") is None
    assert fresh.get_node("OUTSIDE") is not None
