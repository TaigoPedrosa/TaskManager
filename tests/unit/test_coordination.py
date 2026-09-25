import sqlite3
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from taskmanager.core.enums import LockType, NodeKind
from taskmanager.core.models import FileLock, GateRun, Job, Lease, Node
from taskmanager.core.status import Action, JobKind, JobState, Status
from taskmanager.db.cache_repo import CacheRepository
from taskmanager.db.connection import DatabaseManager
from taskmanager.db.job_repo import JobRepository
from taskmanager.db.node_repo import NodeRepository
from taskmanager.db.runtime_repo import RuntimeRepository
from taskmanager.db.schema import SCHEMA_VERSION


@pytest.fixture
def db(tmp_path: Path) -> DatabaseManager:
    mgr = DatabaseManager(tmp_path / ".taskmanager")
    mgr.init_all()
    return mgr


def _task(db: DatabaseManager, node_id: str, status: Status = Status.READY) -> None:
    NodeRepository(db).save_node(Node(id=node_id, kind=NodeKind.TASK, title=node_id, status=status))


def _lease(node_id: str, agent: str = "agent-a", **extra: Any) -> Lease:
    fields: dict[str, Any] = {"action": Action.IMPLEMENT, **extra}
    return Lease(
        task_id=node_id, agent_id=agent, session_id="s", branch_name=f"tm/{node_id}", **fields
    )


def _claimed(node_id: str) -> Node:
    return Node(
        id=node_id,
        kind=NodeKind.TASK,
        title=node_id,
        status=Status.IMPLEMENTING,
        claimed_from=Status.READY,
    )


def _dump(db: DatabaseManager) -> tuple[list[str], list[str]]:
    with db.get_state_connection() as state, db.get_cache_connection() as cache:
        return list(state.iterdump()), list(cache.iterdump())


def test_init_creates_the_cache_and_the_coordination_tables(db: DatabaseManager) -> None:
    with db.get_cache_connection() as conn:
        assert conn.execute("PRAGMA user_version").fetchone() == (SCHEMA_VERSION,)
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"gate_baselines", "condition_results"} <= tables
    with db.get_state_connection() as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"leases", "file_locks", "jobs", "branch_locks"} <= tables


def test_a_lease_keeps_its_action_review_hash_model_and_null_ttl(db: DatabaseManager) -> None:
    repo = RuntimeRepository(db)
    repo.acquire_lease(
        _lease("T1", action=Action.MERGE, review_hash="abc", model="sonnet", ttl_seconds=None),
        [FileLock(file_path="a.py", task_id="T1")],
    )
    loaded = repo.get_lease("T1")
    assert loaded is not None
    assert (loaded.action, loaded.review_hash, loaded.model, loaded.ttl_seconds) == (
        Action.MERGE,
        "abc",
        "sonnet",
        None,
    )


def test_every_lease_and_every_lock_is_listed(db: DatabaseManager) -> None:
    repo = RuntimeRepository(db)
    repo.acquire_lease(
        _lease("T2", agent="agent-b", model="opus"),
        [
            FileLock(file_path="b.py", task_id="T2"),
            FileLock(file_path="a.py", task_id="T2", lock_type=LockType.READ),
        ],
    )
    repo.acquire_lease(_lease("T1"), [])
    assert [(lease.task_id, lease.agent_id, lease.model) for lease in repo.list_leases()] == [
        ("T1", "agent-a", None),
        ("T2", "agent-b", "opus"),
    ]
    assert repo.list_locks() == [
        FileLock(file_path="a.py", task_id="T2", lock_type=LockType.READ),
        FileLock(file_path="b.py", task_id="T2"),
    ]


def test_a_null_ttl_lease_never_expires(db: DatabaseManager) -> None:
    repo = RuntimeRepository(db)
    long_ago = datetime.now(tz=UTC) - timedelta(days=30)
    repo.acquire_lease(
        _lease("T1", ttl_seconds=None, last_heartbeat=long_ago),
        [FileLock(file_path="a.py", task_id="T1")],
    )
    assert repo.sweep_expired_leases() == []
    assert repo.is_file_locked("a.py")
    assert repo.get_conflicting_tasks(["a.py"]) == {"a.py": "Task: T1, Agent: agent-a"}


def test_a_parked_lease_outlives_its_ttl_until_one_agent_takes_it_over(
    db: DatabaseManager,
) -> None:
    repo = RuntimeRepository(db)
    long_ago = datetime.now(tz=UTC) - timedelta(hours=1)
    repo.acquire_lease(
        _lease("T1", action=Action.MERGE, ttl_seconds=60, last_heartbeat=long_ago),
        [FileLock(file_path="a.py", task_id="T1")],
    )
    repo.park("T1")
    assert repo.sweep_expired_leases() == []
    assert repo.take_over("T1", "agent-b", "s2", 900, "sonnet", "tok-b") is True
    lease = repo.get_lease("T1")
    assert lease is not None
    assert (
        lease.agent_id,
        lease.session_id,
        lease.ttl_seconds,
        lease.model,
        lease.action,
        lease.token,
    ) == ("agent-b", "s2", 900, "sonnet", Action.MERGE, "tok-b")
    assert repo.get_conflicting_tasks(["a.py"]) == {"a.py": "Task: T1, Agent: agent-b"}
    assert repo.take_over("T1", "agent-c", "s3", 900, "opus", "tok-c") is False
    assert repo.take_over("T9", "agent-c", "s3", 900, "opus", "tok-c") is False


def test_an_expired_ttl_lease_is_swept_and_frees_its_files(db: DatabaseManager) -> None:
    repo = RuntimeRepository(db)
    long_ago = datetime.now(tz=UTC) - timedelta(hours=1)
    repo.acquire_lease(
        _lease("T1", ttl_seconds=60, last_heartbeat=long_ago),
        [FileLock(file_path="a.py", task_id="T1")],
    )
    assert not repo.is_file_locked("a.py")
    assert repo.sweep_expired_leases() == ["T1"]
    assert repo.get_lease("T1") is None


def test_a_claim_writes_the_lease_the_locks_and_the_status_together(db: DatabaseManager) -> None:
    _task(db, "T1")
    runtime = RuntimeRepository(db)
    assert runtime.claim(_lease("T1"), [FileLock(file_path="a.py", task_id="T1")], _claimed("T1"))
    node = NodeRepository(db).get_node("T1")
    assert node is not None
    assert (node.status, node.claimed_from) == (Status.IMPLEMENTING, Status.READY)
    lease = runtime.get_lease("T1")
    assert lease is not None and lease.action == Action.IMPLEMENT
    assert runtime.get_conflicting_tasks(["a.py"]) == {"a.py": "Task: T1, Agent: agent-a"}


def _status_moved(db: DatabaseManager) -> None:
    _task(db, "T1", status=Status.IMPLEMENTED)


def _lease_exists(db: DatabaseManager) -> None:
    _task(db, "T1")
    RuntimeRepository(db).acquire_lease(_lease("T1", agent="earlier"), [])


def _file_held(db: DatabaseManager) -> None:
    _task(db, "T1")
    _task(db, "T2")
    RuntimeRepository(db).acquire_lease(_lease("T2"), [FileLock(file_path="a.py", task_id="T2")])


def _node_missing(db: DatabaseManager) -> None:
    pass


@pytest.mark.parametrize(
    "arrange",
    [_status_moved, _lease_exists, _file_held, _node_missing],
    ids=["status-moved", "lease-exists", "file-held", "node-missing"],
)
def test_a_refused_claim_leaves_both_databases_unchanged(
    db: DatabaseManager, arrange: Callable[[DatabaseManager], None]
) -> None:
    arrange(db)
    CacheRepository(db).put_condition("T1", 1, "true", 0)
    before = _dump(db)
    claimed = RuntimeRepository(db).claim(
        _lease("T1"), [FileLock(file_path="a.py", task_id="T1")], _claimed("T1")
    )
    assert claimed is False
    assert _dump(db) == before


def test_a_claim_refuses_to_run_inside_an_open_transaction(db: DatabaseManager) -> None:
    _task(db, "T1")
    with pytest.raises(RuntimeError, match="its own transaction"), NodeRepository(db).transaction():
        RuntimeRepository(db).claim(_lease("T1"), [], _claimed("T1"))


def test_a_claim_names_the_status_it_is_claimed_from(db: DatabaseManager) -> None:
    _task(db, "T1")
    unnamed = Node(id="T1", kind=NodeKind.TASK, title="T1").model_copy(
        update={"status": Status.IMPLEMENTING}
    )
    with pytest.raises(ValueError, match="names the status"):
        RuntimeRepository(db).claim(_lease("T1"), [], unnamed)


@pytest.mark.parametrize("attempt", range(20))
def test_two_racing_claims_on_one_node_leave_exactly_one_lease(
    db: DatabaseManager, attempt: int
) -> None:
    node_id = f"T{attempt}"
    _task(db, node_id)
    barrier = threading.Barrier(2)
    results: dict[str, bool] = {}

    def claim(agent: str) -> None:
        lease = _lease(node_id, agent=agent)
        locks = [FileLock(file_path="shared.py", task_id=node_id)]
        barrier.wait()
        results[agent] = RuntimeRepository(db).claim(lease, locks, _claimed(node_id))

    threads = [threading.Thread(target=claim, args=(a,)) for a in ("agent-a", "agent-b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(results.values()) == [False, True]
    winner = next(agent for agent, won in results.items() if won)
    with db.get_state_connection() as conn:
        assert conn.execute("SELECT task_id, agent_id FROM leases").fetchall() == [
            (node_id, winner)
        ]
        assert conn.execute("SELECT file_path, task_id FROM file_locks").fetchall() == [
            ("shared.py", node_id)
        ]
    node = NodeRepository(db).get_node(node_id)
    assert node is not None and node.status == Status.IMPLEMENTING


def test_a_job_gets_an_id_and_reads_back_whole(db: DatabaseManager) -> None:
    _task(db, "T1")
    jobs = JobRepository(db)
    job = jobs.create(
        Job(kind=JobKind.LAND, node_id="T1", repo="core", target="main", step="build")
    )
    assert job.id.startswith("land-")
    assert jobs.get(job.id) == job
    assert jobs.get("land-missing") is None


def test_a_job_update_moves_its_state_and_result(db: DatabaseManager) -> None:
    _task(db, "T1")
    jobs = JobRepository(db)
    job = jobs.create(Job(kind=JobKind.SYNC, node_id="T1", repo="core", target="tm/P"))
    stopped = job.model_copy(
        update={"state": JobState.NEEDS_AGENT, "step": "build", "result": {"reason": "conflict"}}
    )
    jobs.update(stopped)
    assert jobs.get(job.id) == stopped
    with pytest.raises(KeyError):
        jobs.update(job.model_copy(update={"id": "sync-missing"}))


def test_jobs_are_listed_per_node_and_by_waiting_for_an_agent(db: DatabaseManager) -> None:
    _task(db, "T1")
    _task(db, "T2")
    jobs = JobRepository(db)
    running = jobs.create(Job(kind=JobKind.LAND, node_id="T1", repo="core", target="main"))
    waiting = jobs.create(
        Job(
            kind=JobKind.LAND,
            node_id="T1",
            repo="web",
            target="main",
            state=JobState.NEEDS_AGENT,
        )
    )
    other = jobs.create(
        Job(kind=JobKind.SYNC, node_id="T2", repo="core", target="tm/P", state=JobState.NEEDS_AGENT)
    )
    assert jobs.for_node("T1") == [running, waiting]
    assert jobs.waiting_for_agent() == [waiting, other]


@pytest.mark.parametrize("state", list(JobState))
def test_every_job_state_is_stored(db: DatabaseManager, state: JobState) -> None:
    _task(db, "T1")
    jobs = JobRepository(db)
    job = jobs.create(Job(kind=JobKind.LAND, node_id="T1", repo="core", target="main", state=state))
    assert jobs.get(job.id) == job


@pytest.mark.parametrize(
    ("column", "value"), [("kind", "rebase"), ("state", "paused")], ids=["kind", "state"]
)
def test_the_schema_refuses_an_unknown_job_kind_or_state(
    db: DatabaseManager, column: str, value: str
) -> None:
    _task(db, "T1")
    job = JobRepository(db).create(Job(kind=JobKind.LAND, node_id="T1", repo="c", target="main"))
    with db.get_state_connection() as conn, pytest.raises(sqlite3.IntegrityError):
        conn.execute(f"UPDATE jobs SET {column} = ? WHERE id = ?", (value, job.id))


def test_a_branch_lock_has_one_holder_at_a_time(db: DatabaseManager) -> None:
    _task(db, "T1")
    jobs = JobRepository(db)
    first = jobs.create(Job(kind=JobKind.LAND, node_id="T1", repo="core", target="tm/P"))
    second = jobs.create(Job(kind=JobKind.LAND, node_id="T1", repo="core", target="tm/P"))
    assert jobs.acquire_branch("core", "tm/P", first.id) is True
    assert jobs.acquire_branch("core", "tm/P", first.id) is True
    assert jobs.acquire_branch("core", "tm/P", second.id) is False
    assert jobs.acquire_branch("web", "tm/P", second.id) is True
    jobs.release_branch("core", "tm/P", second.id)
    assert jobs.acquire_branch("core", "tm/P", second.id) is False
    jobs.release_branch("core", "tm/P", first.id)
    assert jobs.acquire_branch("core", "tm/P", second.id) is True


def test_a_branch_lock_held_by_a_job_no_longer_running_is_taken_over(
    db: DatabaseManager,
) -> None:
    _task(db, "T1")
    jobs = JobRepository(db)
    dead = jobs.create(Job(kind=JobKind.LAND, node_id="T1", repo="core", target="tm/P"))
    live = jobs.create(Job(kind=JobKind.LAND, node_id="T1", repo="core", target="tm/P"))
    assert jobs.acquire_branch("core", "tm/P", dead.id)
    jobs.update(dead.model_copy(update={"state": JobState.NEEDS_AGENT}))
    assert jobs.acquire_branch("core", "tm/P", live.id) is True


@pytest.mark.parametrize(
    "run",
    [
        GateRun(exit_code=0, failing=None, tail="ok"),
        GateRun(exit_code=1, failing=frozenset({"t::a", "t::b"}), tail="2 failed"),
        GateRun(exit_code=1, failing=frozenset(), tail="collection error"),
    ],
    ids=["green", "red-with-report", "red-empty-report"],
)
def test_a_gate_baseline_reads_back_for_its_repo_sha_and_template(
    db: DatabaseManager, run: GateRun
) -> None:
    cache = CacheRepository(db)
    assert cache.get_baseline("core", "abc", "h1") is None
    cache.put_baseline("core", "abc", "h1", run)
    assert cache.get_baseline("core", "abc", "h1") == run
    assert cache.get_baseline("core", "abc", "h2") is None
    assert cache.get_baseline("core", "abd", "h1") is None
    assert cache.get_baseline("web", "abc", "h1") is None


def test_a_condition_result_is_served_while_fresh_and_for_the_same_command(
    db: DatabaseManager,
) -> None:
    cache = CacheRepository(db)
    assert cache.get_condition("T1", 1, "test -f x", max_age=300) is None
    cache.put_condition("T1", 1, "test -f x", 1)
    assert cache.get_condition("T1", 1, "test -f x", max_age=300) == 1
    assert cache.get_condition("T1", 1, "test -f y", max_age=300) is None
    assert cache.get_condition("T1", 2, "test -f x", max_age=300) is None
    cache.put_condition("T1", 1, "test -f x", 0)
    assert cache.get_condition("T1", 1, "test -f x", max_age=300) == 0


def test_a_condition_result_older_than_its_max_age_is_not_served(db: DatabaseManager) -> None:
    cache = CacheRepository(db)
    cache.put_condition("T1", 1, "true", 0)
    stale = (datetime.now(tz=UTC) - timedelta(seconds=301)).isoformat()
    with db.get_cache_connection() as conn:
        conn.execute("UPDATE condition_results SET checked_at = ?", (stale,))
        conn.commit()
    assert cache.get_condition("T1", 1, "true", max_age=300) is None
    assert cache.get_condition("T1", 1, "true", max_age=400) == 0
