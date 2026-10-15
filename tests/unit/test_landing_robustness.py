import shutil
import signal
import sqlite3
import threading
from pathlib import Path

import pytest
from lifecycle_estate import git, junit_gate, push_main
from test_landing import TRUE, estate_with, reviewed_task

from taskmanager.core.models import Job
from taskmanager.core.status import JobKind, JobState
from taskmanager.engine import landing as landing_module
from taskmanager.engine.claims import Claims
from taskmanager.engine.config import Gate
from taskmanager.engine.landing import Landing
from taskmanager.engine.operations import OperationError


def parked_on_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Claims, Landing, str, Path]:
    """A landing that stopped with reason `error` after its worktree was added, handed to an
    agent; its gate step works again on the next try."""
    claims, landing = estate_with(tmp_path, TRUE)
    reviewed_task(claims)
    real_gate = landing._gate
    failures: list[int] = []

    def gate(job: Job) -> str | JobState:
        if not failures:
            failures.append(1)
            raise RuntimeError("boom")
        return real_gate(job)

    monkeypatch.setattr(landing, "_gate", gate)
    result = claims.start("T1", "merger", "s1")
    assert result.job is not None
    assert landing.run(result.job) == JobState.NEEDS_AGENT
    job = claims.jobs.get(result.job)
    assert job is not None and job.result["reason"] == "error" and job.worktree
    claims.start("T1", "agent", "s1")
    return claims, landing, job.id, Path(job.worktree)


def test_a_resume_that_loses_the_swap_leaves_the_winners_worktree_in_place(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    claims, landing, job_id, worktree = parked_on_error(tmp_path, monkeypatch)
    own = claims.own

    def own_then_lose(*args: object, **kwargs: object) -> object:
        # The other resume wins its swap between this one's read and its own.
        owned = own(*args, **kwargs)  # type: ignore[arg-type]
        winner = claims.jobs.get(job_id)
        assert winner is not None
        winner.state = JobState.RUNNING
        assert claims.jobs.set_state(winner, {JobState.NEEDS_AGENT})
        return owned

    monkeypatch.setattr(claims, "own", own_then_lose)

    with pytest.raises(OperationError, match="moved while it was being resumed"):
        landing.resume(job_id)

    assert worktree.exists()


@pytest.mark.parametrize("left_behind", ["nothing", "a plain directory"])
def test_a_resume_from_error_restarts_when_the_worktree_was_deleted_by_hand(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, left_behind: str
) -> None:
    _claims, landing, job_id, worktree = parked_on_error(tmp_path, monkeypatch)
    shutil.rmtree(worktree)
    if left_behind != "nothing":
        worktree.mkdir()

    assert landing.resume(job_id) == JobState.SUCCEEDED


def test_a_landing_stopped_from_outside_removes_its_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    claims, landing = estate_with(tmp_path, TRUE)
    reviewed_task(claims)

    def stopped(_job: Job) -> str:
        raise SystemExit(143)

    monkeypatch.setattr(landing, "_gate", stopped)
    result = claims.start("T1", "merger", "s1")
    assert result.job is not None
    worktree = claims.root / claims.config.worktree_dir / "land" / result.job

    with pytest.raises(SystemExit):
        landing.run(result.job)

    assert not worktree.exists()
    assert str(worktree) not in git(claims.root / "api", "worktree", "list")


def test_the_landing_process_turns_sigterm_into_an_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[object] = []

    class Stub:
        def run(self, _job_id: str) -> JobState:
            seen.append(signal.getsignal(signal.SIGTERM))
            return JobState.SUCCEEDED

    previous = signal.getsignal(signal.SIGTERM)
    monkeypatch.setattr(Landing, "open", lambda _root, actor=None: Stub())
    try:
        landing_module.main(["run", "job-1", "--root", str(tmp_path)])
    finally:
        signal.signal(signal.SIGTERM, previous)

    handler = seen[0]
    assert callable(handler)
    with pytest.raises(SystemExit) as stopped:
        handler(signal.SIGTERM, None)
    assert stopped.value.code == 128 + signal.SIGTERM


def test_a_failed_heartbeat_does_not_end_the_ones_after_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    claims, landing = estate_with(tmp_path, TRUE)
    beats: list[str] = []
    second = threading.Event()

    def heartbeat(node_id: str) -> None:
        beats.append(node_id)
        if len(beats) == 1:
            raise sqlite3.OperationalError("database is locked")
        second.set()

    monkeypatch.setattr(landing_module, "HEARTBEAT_SECONDS", 0.01)
    monkeypatch.setattr(claims, "heartbeat", heartbeat)

    with landing._beating("T1"):
        assert second.wait(timeout=10)


def baseline_job(claims: Claims) -> Job:
    return claims.jobs.create(
        Job(
            kind=JobKind.LAND,
            node_id="T1",
            repo="api",
            target="main",
            step="gate",
            result={"base_sha": git(claims.root / "api", "rev-parse", "origin/main")},
        )
    )


def cached(claims: Claims, landing: Landing, job: Job, gate: Gate) -> bool:
    from taskmanager.engine import gates

    landing._baseline(job, gate)
    return (
        landing.cache.get_baseline("api", str(job.result["base_sha"]), gates.template_hash(gate))
        is not None
    )


@pytest.mark.parametrize(
    ("command", "is_cached"),
    [("exit 1", False), ("sleep 5", False), ("true", True)],
    ids=["red outside the tests", "timed out", "green"],
)
def test_a_baseline_is_cached_only_when_it_says_what_the_target_does(
    tmp_path: Path, command: str, is_cached: bool
) -> None:
    gate = Gate(command=command, junit=None, timeout=1)
    claims, landing = estate_with(tmp_path, gate)
    reviewed_task(claims)

    assert cached(claims, landing, baseline_job(claims), gate) is is_cached


def test_a_baseline_naming_its_failing_tests_is_cached(tmp_path: Path) -> None:
    gate = junit_gate(tmp_path)
    claims, landing = estate_with(tmp_path, gate)
    push_main(claims.root / "api", "failing.txt", "a\n")
    reviewed_task(claims)

    assert cached(claims, landing, baseline_job(claims), gate)
