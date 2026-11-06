import subprocess
import sys
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from lifecycle_estate import (
    add,
    attach_landing,
    branch_at,
    commit,
    gate_runs,
    gated,
    git,
    junit_gate,
    make_estate,
    on_branch,
    push_main,
    section,
    stored,
)

from taskmanager.core.enums import NodeKind, VerificationType
from taskmanager.core.models import Condition, Job, NodeVerification
from taskmanager.core.status import Action, ConditionStage, JobState, Merge, Outcome, Status
from taskmanager.engine import git as landing_git
from taskmanager.engine.claims import Claims, Verified
from taskmanager.engine.config import Gate, ProjectConfig, RepoConfig
from taskmanager.engine.landing import Landing
from taskmanager.engine.operations import OperationError

Seen = tuple[dict[str, JobState], Status, str]

TRUE = Gate(command="true", junit=None, timeout=60)


def estate_with(tmp_path: Path, gate: Gate, **config: object) -> tuple[Claims, Landing]:
    repos = {"api": RepoConfig(push=True, gates={"main": gate})}
    claims = make_estate(tmp_path, config=ProjectConfig(repos=repos, **config))
    return claims, attach_landing(claims)


def reviewed_task(
    claims: Claims, node_id: str = "T1", path: str = "feature.py", content: str = "x = 1\n"
) -> None:
    add(claims, node_id, status=Status.REVIEWED, outcome=Outcome.APPROVE, review_cycles=1)
    on_branch(claims.root / "api", f"tm/{node_id}", path, content)


def verification(claims: Claims, node_id: str, path: str) -> None:
    claims.nodes.add_verification(
        NodeVerification(
            node_id=node_id, verification_type=VerificationType.FILE_EXISTS, target_path=path
        )
    )


def land(claims: Claims, landing: Landing, node_id: str = "T1") -> tuple[str, JobState]:
    result = claims.start(node_id, "merger", "s1")
    assert result.action == Action.MERGE, result.reason
    assert result.job is not None
    return result.job, landing.run(result.job)


def land_ungated(claims: Claims, landing: Landing) -> tuple[str, JobState]:
    """A landing claimed while its repository has a main gate that is then removed."""
    merge = claims.start("T1", "merger", "s1")
    assert merge.job is not None, merge.reason
    claims.config.repos["api"] = RepoConfig(push=True)
    return merge.job, landing.run(merge.job)


def merges_of(api: Path, node_id: str) -> int:
    subjects = git(api, "log", "--first-parent", "--format=%s", "origin/main").splitlines()
    return subjects.count(f"merge({node_id}): land tm/{node_id} on main")


def test_a_clean_landing_merges_gates_pushes_verifies_and_completes(tmp_path: Path) -> None:
    claims, landing = estate_with(tmp_path, TRUE)
    api = claims.root / "api"
    reviewed_task(claims)
    verification(claims, "T1", "api/feature.py")

    job_id, state = land(claims, landing)

    assert job_id.startswith("land-")
    assert state == JobState.SUCCEEDED
    assert stored(claims, "T1").status == Status.COMPLETED
    assert claims.runtime.get_lease("T1") is None
    assert git(api, "log", "-1", "--format=%s", "origin/main") == "merge(T1): land tm/T1 on main"
    assert git(api, "show", "origin/main:feature.py") == "x = 1"
    job = claims.jobs.get(job_id)
    assert job is not None and job.worktree is None
    assert "PASS" in section(claims, "T1", "merge")


def test_a_branch_already_on_its_target_completes_without_a_new_commit(tmp_path: Path) -> None:
    claims, landing = estate_with(tmp_path, TRUE)
    api = claims.root / "api"
    add(claims, "T1", status=Status.REVIEWED, outcome=Outcome.APPROVE)
    branch_at(api, "tm/T1")
    before = git(api, "ls-remote", "origin", "refs/heads/main")

    assert land(claims, landing)[1] == JobState.SUCCEEDED
    assert stored(claims, "T1").status == Status.COMPLETED
    assert git(api, "ls-remote", "origin", "refs/heads/main") == before


def test_a_branch_whose_tree_matches_target_but_not_its_history_lands_without_a_merge_commit(
    tmp_path: Path,
) -> None:
    """A branch can carry a real commit forward and revert it, ending on the same tree as its
    target without ever being a git ancestor of it: still nothing to land."""
    claims, landing = estate_with(tmp_path, TRUE)
    api = claims.root / "api"
    add(claims, "T1", status=Status.REVIEWED, outcome=Outcome.APPROVE)
    on_branch(api, "tm/T1", "feature.py", "x = 1\n")
    on_branch(api, "tm/T1", "feature.py", None)
    before = git(api, "ls-remote", "origin", "refs/heads/main")

    assert land(claims, landing)[1] == JobState.SUCCEEDED
    assert stored(claims, "T1").status == Status.COMPLETED
    assert git(api, "ls-remote", "origin", "refs/heads/main") == before
    assert merges_of(api, "T1") == 0


def test_a_conflict_is_handed_to_an_agent_who_resolves_commits_and_resumes(tmp_path: Path) -> None:
    claims, landing = estate_with(tmp_path, TRUE)
    api = claims.root / "api"
    reviewed_task(claims, path="app.py", content="branch\n")
    push_main(api, "app.py", "main\n")

    job_id, state = land(claims, landing)

    assert state == JobState.NEEDS_AGENT
    job = claims.jobs.get(job_id)
    assert job is not None and job.result["reason"] == "conflict"
    lease = claims.runtime.get_lease("T1")
    assert lease is not None and lease.ttl_seconds is None
    assert stored(claims, "T1").status == Status.MERGING

    handed = claims.start("T1", "resolver", "s2")
    assert (handed.action, handed.job, handed.worktree) == (Action.MERGE, job_id, job.worktree)
    assert handed.worktree is not None
    worktree = Path(handed.worktree)
    with pytest.raises(OperationError, match="not committed"):
        landing.resume(job_id)
    (worktree / "app.py").write_text("both\n")
    git(worktree, "add", "app.py")
    git(worktree, "commit", "-q", "--no-edit")

    assert landing.resume(job_id) == JobState.SUCCEEDED
    assert stored(claims, "T1").status == Status.COMPLETED
    assert git(api, "show", "origin/main:app.py") == "both"


def resolved_conflict(tmp_path: Path) -> tuple[Claims, Landing, str, Path]:
    claims, landing = estate_with(tmp_path, TRUE)
    reviewed_task(claims, path="app.py", content="branch\n")
    push_main(claims.root / "api", "app.py", "main\n")
    job_id, state = land(claims, landing)
    assert state == JobState.NEEDS_AGENT
    handed = claims.start("T1", "resolver", "s2")
    assert handed.worktree is not None
    worktree = Path(handed.worktree)
    (worktree / "app.py").write_text("both\n")
    git(worktree, "add", "app.py")
    git(worktree, "commit", "-q", "--no-edit")
    return claims, landing, job_id, worktree


def test_a_resume_ignores_untracked_files_beside_a_committed_merge(tmp_path: Path) -> None:
    claims, landing, job_id, worktree = resolved_conflict(tmp_path)
    (worktree / "report.xml").write_text("<testsuites/>\n")

    assert landing.resume(job_id) == JobState.SUCCEEDED
    assert stored(claims, "T1").status == Status.COMPLETED


def test_a_resume_refuses_a_tracked_change_left_uncommitted_after_the_merge(
    tmp_path: Path,
) -> None:
    claims, landing, job_id, worktree = resolved_conflict(tmp_path)
    (worktree / "app.py").write_text("edited\n")

    with pytest.raises(OperationError, match="not committed") as refused:
        landing.resume(job_id)
    assert refused.value.status_code == 409
    assert stored(claims, "T1").status == Status.MERGING


MOVER = """
import pathlib, subprocess, sys
api, flag, log = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2]), pathlib.Path(sys.argv[3])
with log.open("a") as out:
    out.write("gate\\n")
if not flag.exists():
    flag.touch()
    subprocess.run(["git", "-C", str(api), "push", "-q", "origin", "side:main"], check=True)
"""


def test_a_target_that_moves_during_the_gate_is_merged_in_and_gated_again(tmp_path: Path) -> None:
    script = tmp_path / "mover.py"
    script.write_text(MOVER)
    api_path = tmp_path / "estate" / "api"
    command = f"{sys.executable} {script} {api_path} {tmp_path / 'moved'} {tmp_path / 'gate.log'}"
    claims, landing = estate_with(tmp_path, Gate(command=command, junit=None, timeout=60))
    api = claims.root / "api"
    reviewed_task(claims)
    side = on_branch(api, "side", "other.py", "y = 2\n")

    assert land(claims, landing)[1] == JobState.SUCCEEDED
    assert gate_runs(tmp_path) == ["gate", "gate"]
    git(api, "merge-base", "--is-ancestor", side, "origin/main")
    assert git(api, "show", "origin/main:feature.py") == "x = 1"


@pytest.mark.parametrize(
    ("on_main", "on_tip", "state", "status", "attempts"),
    [
        ("a b", "a", JobState.SUCCEEDED, Status.COMPLETED, 0),
        ("a", "a", JobState.CONDITION_UNMET, Status.REVIEWED, 0),
        ("a", "a c", JobState.OWN_DEFECT, Status.REVIEWED, 1),
        ("", "c", JobState.OWN_DEFECT, Status.REVIEWED, 1),
    ],
)
def test_a_red_tip_is_pushed_parked_or_charged_by_its_junit_set_against_the_baseline(
    tmp_path: Path, on_main: str, on_tip: str, state: JobState, status: Status, attempts: int
) -> None:
    claims, landing = estate_with(tmp_path, junit_gate(tmp_path))
    api = claims.root / "api"
    if on_main:
        push_main(api, "failing.txt", on_main + "\n")
    reviewed_task(claims)
    if on_tip != on_main:
        on_branch(api, "tm/T1", "failing.txt", on_tip + "\n")

    _, result = land(claims, landing)

    node = stored(claims, "T1")
    assert (result, node.status, node.merge_attempts, node.step_failures) == (
        state,
        status,
        attempts,
        0,
    )
    if attempts:
        assert node.outcome == Outcome.MERGE_FAILED
    parked = [c for c in claims.nodes.get_conditions("T1") if c.needs.startswith("red-target")]
    assert len(parked) == (1 if state == JobState.CONDITION_UNMET else 0)


def test_one_baseline_run_serves_every_landing_on_the_same_target_sha(tmp_path: Path) -> None:
    claims, landing = estate_with(tmp_path, junit_gate(tmp_path))
    push_main(claims.root / "api", "failing.txt", "a\n")
    reviewed_task(claims, "T1")
    reviewed_task(claims, "T2", path="other.py")

    assert land(claims, landing, "T1")[1] == JobState.CONDITION_UNMET
    assert land(claims, landing, "T2")[1] == JobState.CONDITION_UNMET

    runs = gate_runs(tmp_path)
    assert len(runs) == 3
    assert sum(run.endswith("-base") for run in runs) == 1


def test_a_landing_parked_on_a_red_main_waits_until_main_moves_then_lands(tmp_path: Path) -> None:
    claims, landing = estate_with(tmp_path, junit_gate(tmp_path), condition_ttl=1)
    api = claims.root / "api"
    push_main(api, "failing.txt", "a\n")
    reviewed_task(claims)
    assert land(claims, landing)[1] == JobState.CONDITION_UNMET

    blocked = claims.start("T1", "merger", "s1")
    assert blocked.action == Action.BLOCKED
    assert "red-target" in (blocked.reason or "")

    push_main(api, "failing.txt", None)
    time.sleep(1.1)
    assert land(claims, landing)[1] == JobState.SUCCEEDED
    assert claims.nodes.get_conditions("T1") == []


def test_landings_parked_on_one_red_main_past_the_threshold_reach_the_owner_as_one_decision(
    tmp_path: Path,
) -> None:
    claims, _ = park_two_on_a_red_main_long_ago(tmp_path)

    claims.sweep()
    claims.sweep()

    decisions = {
        dep
        for node_id in ("T1", "T2")
        for dep in claims.nodes.get_dependencies(node_id)
        if stored(claims, dep).kind == NodeKind.DECISION
    }
    assert len(decisions) == 1
    assert all(next(iter(decisions)) in claims.nodes.get_dependencies(n) for n in ("T1", "T2"))


def test_an_unattributed_red_is_handed_to_an_agent_who_may_push_it(tmp_path: Path) -> None:
    claims, landing = estate_with(tmp_path, Gate(command="exit 1", junit=None, timeout=60))
    reviewed_task(claims)

    job_id, state = land(claims, landing)

    assert state == JobState.NEEDS_AGENT
    job = claims.jobs.get(job_id)
    assert job is not None
    assert job.result["reason"] == "unattributed"
    assert "tip" in job.result and "base" in job.result
    assert claims.start("T1", "judge", "s2").action == Action.MERGE
    assert landing.resume(job_id, push=True) == JobState.SUCCEEDED
    assert stored(claims, "T1").status == Status.COMPLETED


def test_a_verification_red_after_landing_is_an_own_defect(tmp_path: Path) -> None:
    claims, landing = estate_with(tmp_path, TRUE)
    api = claims.root / "api"
    reviewed_task(claims)
    verification(claims, "T1", "api/missing.py")

    assert land(claims, landing)[1] == JobState.OWN_DEFECT

    node = stored(claims, "T1")
    assert (node.status, node.outcome, node.merge_attempts) == (
        Status.REVIEWED,
        Outcome.MERGE_FAILED,
        1,
    )
    assert git(api, "show", "origin/main:feature.py") == "x = 1"
    assert "FAIL" in section(claims, "T1", "merge")


def slow_until(claims: Claims, flag: Path) -> None:
    """A check that outlives a 1 second timeout until `flag` exists, as one queued behind a
    shared runner would."""
    claims.nodes.add_verification(
        NodeVerification(
            node_id="T1",
            verification_type=VerificationType.TEST_COMMAND,
            target_path="waits for a runner",
            expected_pattern=f"test -f {flag} || sleep 5",
        )
    )


def test_a_verification_that_times_out_after_landing_waits_for_an_agent_and_resumes_at_verify(
    tmp_path: Path,
) -> None:
    logged = Gate(command=f"echo gate >> {tmp_path / 'gate.log'}", junit=None, timeout=60)
    claims, landing = estate_with(tmp_path, logged, verify_timeout=1)
    api = claims.root / "api"
    reviewed_task(claims)
    slow_until(claims, tmp_path / "free")

    job_id, state = land(claims, landing)

    assert state == JobState.NEEDS_AGENT
    job = claims.jobs.get(job_id)
    assert job is not None
    assert job.result["reason"] == "verify timeout"
    assert "timed out after 1s" in job.result["report"]
    assert stored(claims, "T1").outcome != Outcome.MERGE_FAILED
    (tmp_path / "free").touch()
    assert claims.start("T1", "agent", "s2").action == Action.MERGE
    assert landing.resume(job_id) == JobState.SUCCEEDED
    assert stored(claims, "T1").status == Status.COMPLETED
    assert merges_of(api, "T1") == 1
    assert gate_runs(tmp_path) == ["gate"]


def test_a_landing_killed_after_its_push_is_swept_back_and_the_next_completes_without_a_second_merge(
    tmp_path: Path,
) -> None:
    claims, landing = estate_with(tmp_path, TRUE)
    api = claims.root / "api"
    reviewed_task(claims)
    first = claims.start("T1", "merger", "s1", ttl=1)
    assert first.job is not None
    killed = tmp_path / "killed"
    git(api, "worktree", "add", "-q", "--detach", str(killed), "origin/main")
    git(killed, "merge", "-q", "--no-ff", "-m", "merge(T1): land tm/T1 on main", "tm/T1")
    git(killed, "push", "-q", "origin", "HEAD:main")
    time.sleep(1.5)

    assert claims.sweep() == ["T1"]
    dead = claims.jobs.get(first.job)
    assert dead is not None and dead.state == JobState.EXPIRED
    node = stored(claims, "T1")
    assert (node.status, node.step_failures) == (Status.REVIEWED, 1)

    second = claims.start("T1", "merger", "s1")
    assert second.job is not None
    assert landing.run(second.job) == JobState.SUCCEEDED
    assert stored(claims, "T1").status == Status.COMPLETED
    git(api, "fetch", "-q", "origin")
    assert merges_of(api, "T1") == 1


@contextmanager
def watching(
    claims: Claims, landing: Landing, monkeypatch: pytest.MonkeyPatch, node_id: str
) -> Iterator[list[Seen]]:
    """What another connection reads after every job state write: the landing's own thread
    would see its uncommitted writes, which no other reader can."""
    seen: list[Seen] = []
    end = landing._end

    def read() -> Seen:
        jobs = {j.id: j.state for j in claims.jobs.for_node(node_id)}
        return jobs, stored(claims, node_id).status, section(claims, node_id, "merge")

    with ThreadPoolExecutor(max_workers=1) as reader:

        def observed(job: Job, state: JobState, **result: object) -> bool:
            ended = end(job, state, **result)
            seen.append(reader.submit(read).result())
            return ended

        monkeypatch.setattr(landing, "_end", observed)
        yield seen
        seen.append(reader.submit(read).result())


def test_succeed_writes_the_node_completed_no_later_than_the_job_reads_succeeded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    claims, landing = estate_with(tmp_path, TRUE)
    reviewed_task(claims)

    with watching(claims, landing, monkeypatch, "T1") as seen:
        job_id, state = land(claims, landing)

    assert state == JobState.SUCCEEDED
    first = next(s for s in seen if s[0][job_id] == JobState.SUCCEEDED)
    assert first[1] == Status.COMPLETED


def test_succeed_writes_the_merge_note_and_next_job_no_later_than_the_first_repository_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gates = {repo: RepoConfig(push=True, gates={"main": TRUE}) for repo in ("api", "web")}
    claims = make_estate(tmp_path, repos=("api", "web"), config=ProjectConfig(repos=gates))
    landing = attach_landing(claims)
    add(claims, "P", NodeKind.PLAN, status=Status.REVIEWED, outcome=Outcome.APPROVE)
    add(claims, "A", parent="P", repo="api", merge=Merge.PARENT, status=Status.COMPLETED)
    add(claims, "W", parent="P", repo="web", merge=Merge.PARENT, status=Status.COMPLETED)
    on_branch(claims.root / "api", "tm/P", "a.py", "a = 1\n")
    on_branch(claims.root / "web", "tm/P", "w.py", "w = 1\n")

    with watching(claims, landing, monkeypatch, "P") as seen:
        job_id, state = land(claims, landing, "P")

    assert state == JobState.SUCCEEDED
    jobs, _, merge = next(s for s in seen if s[0][job_id] == JobState.SUCCEEDED)
    assert len(jobs) == 2
    assert "api: landed on main" in merge
    assert stored(claims, "P").status == Status.COMPLETED


def test_succeed_writes_nothing_when_the_job_moved_before_it_ends(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    claims, landing = estate_with(tmp_path, TRUE)
    reviewed_task(claims)
    verify = claims.verify

    def expiring(node_id: str, ref: str, repo: str | None) -> Verified:
        for job in claims.jobs.for_node(node_id):
            claims.jobs.set_state(job.model_copy(update={"state": JobState.EXPIRED}))
        return verify(node_id, ref, repo)

    monkeypatch.setattr(claims, "verify", expiring)
    job_id, state = land(claims, landing)

    assert state == JobState.EXPIRED
    assert stored(claims, "T1").status == Status.MERGING
    assert section(claims, "T1", "merge") == ""
    assert [j.id for j in claims.jobs.for_node("T1")] == [job_id]


def test_succeed_writes_neither_when_the_node_refuses_to_land(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    claims, landing = estate_with(tmp_path, TRUE)
    reviewed_task(claims)

    def refused(node_id: str, report: str) -> Status:
        raise OperationError(f"{node_id} cannot land", 409)

    monkeypatch.setattr(claims, "landed", refused)
    job_id, state = land(claims, landing)

    assert state == JobState.NEEDS_AGENT
    job = claims.jobs.get(job_id)
    assert job is not None and job.result["reason"] == "error"
    assert stored(claims, "T1").status == Status.MERGING


def test_the_module_entry_point_runs_a_landing_as_a_detached_process(tmp_path: Path) -> None:
    claims = make_estate(
        tmp_path, config=ProjectConfig(repos={"api": RepoConfig(push=True, gates={"main": TRUE})})
    )
    attach_landing(claims, detach=True)
    reviewed_task(claims)

    result = claims.start("T1", "merger", "s1")
    assert result.job is not None
    deadline = time.monotonic() + 60
    job = claims.jobs.get(result.job)
    while job is not None and job.state == JobState.RUNNING and time.monotonic() < deadline:
        time.sleep(0.2)
        job = claims.jobs.get(result.job)

    log = claims.root / ".taskmanager" / "jobs" / f"{result.job}.log"
    assert job is not None and job.state == JobState.SUCCEEDED, log.read_text()
    assert stored(claims, "T1").status == Status.COMPLETED


def test_a_landing_condition_unmet_when_the_job_runs_returns_the_node_without_counting(
    tmp_path: Path,
) -> None:
    claims, landing = estate_with(tmp_path, TRUE)
    reviewed_task(claims)
    result = claims.start("T1", "merger", "s1")
    assert result.job is not None
    claims.nodes.add_condition(
        Condition(
            node_id="T1",
            idx=0,
            needs="release window open",
            command=f"test -f {tmp_path / 'window'}",
            stage=ConditionStage.LANDING,
        )
    )

    assert landing.run(result.job) == JobState.CONDITION_UNMET

    node = stored(claims, "T1")
    assert (node.status, node.step_failures, node.merge_attempts) == (Status.REVIEWED, 0, 0)
    again = claims.start("T1", "merger", "s1")
    assert again.action == Action.BLOCKED
    assert "release window open" in (again.reason or "")


def test_a_repository_with_no_main_gate_stops_for_an_agent(tmp_path: Path) -> None:
    claims, landing = estate_with(tmp_path, TRUE)
    reviewed_task(claims)

    job_id, state = land_ungated(claims, landing)

    job = claims.jobs.get(job_id)
    assert state == JobState.NEEDS_AGENT
    assert job is not None and job.result["reason"] == "no gate"


def parent_landing(claims: Claims, **columns: object) -> Path:
    api = claims.root / "api"
    add(claims, "P", NodeKind.PLAN, review=True, fix=True)
    add(
        claims,
        "T1",
        parent="P",
        merge=Merge.PARENT,
        status=Status.REVIEWED,
        review_cycles=1,
        **{"outcome": Outcome.APPROVE, **columns},
    )
    branch_at(api, "tm/P")
    on_branch(api, "tm/T1", "feature.py", "x = 1\n", base="tm/P")
    return api


def test_a_child_lands_on_its_parent_branch_after_its_own_verifications(tmp_path: Path) -> None:
    claims, landing = estate_with(tmp_path, TRUE)
    api = parent_landing(claims)
    verification(claims, "T1", "api/feature.py")
    main_before = git(api, "ls-remote", "origin", "refs/heads/main")

    assert land(claims, landing)[1] == JobState.SUCCEEDED

    assert git(api, "show", "tm/P:feature.py") == "x = 1"
    assert git(api, "ls-remote", "origin", "refs/heads/main") == main_before
    assert stored(claims, "T1").status == Status.COMPLETED
    assert stored(claims, "P").status == Status.IMPLEMENTED


@pytest.mark.parametrize(
    ("fix", "outcome", "state"),
    [
        (True, Outcome.APPROVE, JobState.OWN_DEFECT),
        (False, Outcome.REJECT, JobState.SUCCEEDED),
    ],
)
def test_own_verifications_stop_a_parent_landing_unless_nobody_below_fixes_a_rejection(
    tmp_path: Path, fix: bool, outcome: Outcome, state: JobState
) -> None:
    claims, landing = estate_with(tmp_path, TRUE)
    parent_landing(claims, fix=fix, outcome=outcome)
    verification(claims, "T1", "api/missing.py")

    assert land(claims, landing)[1] == state
    assert "FAIL" in section(claims, "T1", "merge")


def test_own_verifications_that_time_out_at_the_merge_wait_for_an_agent(tmp_path: Path) -> None:
    claims, landing = estate_with(tmp_path, TRUE, verify_timeout=1)
    api = parent_landing(claims)
    slow_until(claims, tmp_path / "free")

    job_id, state = land(claims, landing)

    assert state == JobState.NEEDS_AGENT
    job = claims.jobs.get(job_id)
    assert job is not None
    assert job.result["reason"] == "verify timeout"
    (tmp_path / "free").touch()
    assert claims.start("T1", "agent", "s2").action == Action.MERGE
    assert landing.resume(job_id) == JobState.SUCCEEDED
    assert git(api, "show", "tm/P:feature.py") == "x = 1"


def test_a_parent_branch_that_moves_before_the_swap_is_merged_in_and_gated_again(
    tmp_path: Path,
) -> None:
    claims, landing = estate_with(tmp_path, TRUE)
    api = parent_landing(claims)
    side = on_branch(api, "side", "side.py", "s = 1\n", base="tm/P")
    flag = tmp_path / "moved"
    move = f"test -f {flag} || {{ touch {flag}; git -C {api} update-ref refs/heads/tm/P {side}; }}"
    claims.nodes.add_verification(
        NodeVerification(
            node_id="T1",
            verification_type=VerificationType.TEST_COMMAND,
            target_path="moves tm/P once",
            expected_pattern=move,
        )
    )

    assert land(claims, landing)[1] == JobState.SUCCEEDED

    tip = git(api, "rev-parse", "tm/P")
    git(api, "merge-base", "--is-ancestor", side, tip)
    git(api, "merge-base", "--is-ancestor", "tm/T1", tip)


def release_from_the_gate(root: Path) -> Gate:
    """A green gate that first releases T1 from another process, as an agent's release that
    lands while the gate runs would."""
    script = (
        "from pathlib import Path; from taskmanager.engine.claims import Claims; "
        f"Claims.open(Path({str(root)!r})).release('T1')"
    )
    return Gate(command=f'{sys.executable} -c "{script}"', junit=None, timeout=60)


def test_a_landing_released_mid_gate_stays_expired_and_pushes_nothing(tmp_path: Path) -> None:
    claims, landing = estate_with(tmp_path, TRUE)
    claims.config.repos["api"] = RepoConfig(
        push=True, gates={"main": release_from_the_gate(claims.root)}
    )
    api = claims.root / "api"
    reviewed_task(claims)
    before = git(api, "ls-remote", "origin", "refs/heads/main")

    job_id, state = land(claims, landing)

    assert state == JobState.EXPIRED
    job = claims.jobs.get(job_id)
    assert job is not None and job.state == JobState.EXPIRED
    assert git(api, "ls-remote", "origin", "refs/heads/main") == before
    node = stored(claims, "T1")
    assert (node.status, node.step_failures) == (Status.REVIEWED, 1)


def _running(pid: int) -> bool:
    import subprocess

    stat = subprocess.run(
        ["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True, check=False
    ).stdout.strip()
    return bool(stat) and not stat.startswith("Z")


def test_releasing_a_running_landing_stops_its_process(tmp_path: Path) -> None:
    claims = make_estate(
        tmp_path,
        config=ProjectConfig(
            repos={
                "api": RepoConfig(push=True, gates={"main": Gate(command="sleep 3", timeout=60)})
            }
        ),
    )
    attach_landing(claims, detach=True)
    api = claims.root / "api"
    reviewed_task(claims)
    before = git(api, "ls-remote", "origin", "refs/heads/main")
    result = claims.start("T1", "merger", "s1")
    assert result.job is not None
    deadline = time.monotonic() + 30
    job = claims.jobs.get(result.job)
    while job is not None and job.step != "gate" and time.monotonic() < deadline:
        time.sleep(0.1)
        job = claims.jobs.get(result.job)
    assert job is not None and job.step == "gate" and job.pid is not None

    claims.release("T1")

    deadline = time.monotonic() + 10
    while _running(job.pid) and time.monotonic() < deadline:
        time.sleep(0.1)
    assert not _running(job.pid)
    stopped = claims.jobs.get(result.job)
    assert stopped is not None and stopped.state == JobState.EXPIRED
    assert git(api, "ls-remote", "origin", "refs/heads/main") == before


def test_a_landing_that_cannot_be_launched_undoes_the_claim_and_expires_its_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    claims, landing = estate_with(tmp_path, TRUE)
    reviewed_task(claims)

    def unlaunchable(job: object) -> None:
        raise OSError("fork failed")

    monkeypatch.setattr(landing, "_launch", unlaunchable)
    with pytest.raises(OperationError, match="undone"):
        claims.start("T1", "merger", "s1")

    assert [j.state for j in claims.jobs.for_node("T1")] == [JobState.EXPIRED]
    assert stored(claims, "T1").status == Status.REVIEWED
    monkeypatch.undo()
    assert land(claims, landing)[1] == JobState.SUCCEEDED


def test_a_landing_parked_on_a_red_parent_branch_waits_until_that_branch_moves(
    tmp_path: Path,
) -> None:
    claims = make_estate(
        tmp_path,
        config=ProjectConfig(
            repos={
                "api": RepoConfig(push=True, gates={"main": TRUE, "parent": junit_gate(tmp_path)})
            },
            condition_ttl=1,
        ),
    )
    landing = attach_landing(claims)
    api = parent_landing(claims)
    on_branch(api, "tm/P", "failing.txt", "a\n")
    assert land(claims, landing)[1] == JobState.CONDITION_UNMET

    time.sleep(1.1)
    blocked = claims.start("T1", "merger", "s1")
    assert blocked.action == Action.BLOCKED, blocked
    assert "red-target" in (blocked.reason or "")

    on_branch(api, "tm/P", "failing.txt", None)
    time.sleep(1.1)
    assert land(claims, landing)[1] == JobState.SUCCEEDED
    assert claims.nodes.get_conditions("T1") == []


def park_two_on_a_red_main_long_ago(tmp_path: Path) -> tuple[Claims, Landing]:
    claims, landing = estate_with(tmp_path, junit_gate(tmp_path))
    push_main(claims.root / "api", "failing.txt", "a\n")
    reviewed_task(claims, "T1")
    reviewed_task(claims, "T2", path="other.py")
    land(claims, landing, "T1")
    land(claims, landing, "T2")
    two_hours_ago = (datetime.now(tz=UTC) - timedelta(hours=2)).isoformat()
    for node_id in ("T1", "T2"):
        for job in claims.jobs.for_node(node_id):
            job.result["red_target"]["since"] = two_hours_ago
            claims.jobs.set_state(job)
    return claims, landing


def test_a_parked_node_mid_step_is_left_out_of_the_red_main_decision_and_the_sweep_goes_on(
    tmp_path: Path,
) -> None:
    from taskmanager.core.models import Lease

    claims, _ = park_two_on_a_red_main_long_ago(tmp_path)
    claims.runtime.acquire_lease(
        Lease(task_id="T2", agent_id="busy", session_id="s", branch_name="tm/T2", ttl_seconds=600),
        [],
    )

    claims.sweep()

    decisions = [dep for dep in claims.nodes.get_dependencies("T1") if dep.startswith("decision-")]
    assert len(decisions) == 1
    assert not [d for d in claims.nodes.get_dependencies("T2") if d.startswith("decision-")]


@pytest.mark.parametrize("repair", ["reset", "reopen"])
def test_a_repair_ends_a_parked_landing_s_red_target_wait(tmp_path: Path, repair: str) -> None:
    claims, landing = estate_with(tmp_path, junit_gate(tmp_path))
    push_main(claims.root / "api", "failing.txt", "a\n")
    reviewed_task(claims)
    land(claims, landing)
    assert claims.nodes.get_conditions("T1")

    if repair == "reset":
        claims.reset("T1", Status.READY, "start over")
    else:
        claims.defer("T1", "later")
        claims.reopen("T1", "back")

    assert claims.nodes.get_conditions("T1") == []


def test_a_handed_over_job_that_stops_again_counts_a_step_failure_until_the_node_fails(
    tmp_path: Path,
) -> None:
    claims, landing = estate_with(tmp_path, TRUE)
    reviewed_task(claims)
    job_id, state = land_ungated(claims, landing)
    assert state == JobState.NEEDS_AGENT
    assert stored(claims, "T1").step_failures == 0

    for failures in (1, 2):
        assert claims.start("T1", "agent", "s1").action == Action.MERGE
        assert landing.resume(job_id) == JobState.NEEDS_AGENT
        node = stored(claims, "T1")
        assert (node.status, node.step_failures) == (Status.MERGING, failures)
        lease = claims.runtime.get_lease("T1")
        assert lease is not None and lease.ttl_seconds is None

    assert claims.start("T1", "agent", "s1").action == Action.MERGE
    landing.resume(job_id)
    node = stored(claims, "T1")
    assert (node.status, node.step_failures) == (Status.FAILED, 3)
    assert claims.runtime.get_lease("T1") is None
    job = claims.jobs.get(job_id)
    assert job is not None and job.state == JobState.EXPIRED


def test_a_task_whose_branch_is_missing_stops_for_an_agent_instead_of_completing(
    tmp_path: Path,
) -> None:
    claims, landing = estate_with(tmp_path, TRUE)
    add(claims, "T1", status=Status.REVIEWED, outcome=Outcome.APPROVE, review_cycles=1)

    job_id, state = land(claims, landing)

    assert state == JobState.NEEDS_AGENT
    job = claims.jobs.get(job_id)
    assert job is not None and job.result["reason"] == "no branch"
    assert stored(claims, "T1").status == Status.MERGING


def test_two_landings_reaching_one_uncached_baseline_at_once_share_one_run(
    tmp_path: Path,
) -> None:
    import threading

    slow = junit_gate(tmp_path)
    slow = Gate(command=f"sleep 1 && {slow.command}", junit=slow.junit, timeout=60)
    claims, landing = estate_with(tmp_path, slow)
    push_main(claims.root / "api", "failing.txt", "a\n")
    reviewed_task(claims, "T1")
    reviewed_task(claims, "T2", path="other.py")
    jobs = [claims.start(node_id, "merger", "s1").job for node_id in ("T1", "T2")]
    states: list[JobState] = []

    def run(job_id: str | None) -> None:
        assert job_id is not None
        states.append(landing.run(job_id))

    threads = [threading.Thread(target=run, args=(job_id,)) for job_id in jobs]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert states == [JobState.CONDITION_UNMET, JobState.CONDITION_UNMET]
    assert sum(run.endswith("-base") for run in gate_runs(tmp_path)) == 1


def test_a_landing_starts_only_under_a_merge_claim_and_never_beside_a_live_one(
    tmp_path: Path,
) -> None:
    claims, landing = estate_with(tmp_path, TRUE)
    api = claims.root / "api"
    reviewed_task(claims)
    before = git(api, "ls-remote", "origin", "refs/heads/main")

    with pytest.raises(OperationError, match="merge claim"):
        landing.start_land("T1")
    assert claims.jobs.for_node("T1") == []

    result = claims.start("T1", "merger", "s1")
    assert result.action == Action.MERGE and result.job is not None
    with pytest.raises(OperationError, match="already landing"):
        landing.start_land("T1")
    assert [j.id for j in claims.jobs.for_node("T1")] == [result.job]
    assert git(api, "ls-remote", "origin", "refs/heads/main") == before

    assert landing.run(result.job) == JobState.SUCCEEDED


def test_a_handed_over_job_resumes_only_for_the_agent_and_token_it_was_handed_to(
    tmp_path: Path,
) -> None:
    claims, landing = estate_with(tmp_path, TRUE)
    reviewed_task(claims, path="app.py", content="branch\n")
    push_main(claims.root / "api", "app.py", "main\n")
    job_id, state = land(claims, landing)
    assert state == JobState.NEEDS_AGENT

    handed = claims.start("T1", "resolver", "s2")
    assert handed.token is not None
    with pytest.raises(OperationError, match="someone holds no live lease"):
        landing.resume(job_id, agent="someone")
    with pytest.raises(OperationError, match="another claim holds it"):
        landing.resume(job_id, token="stale")
    job = claims.jobs.get(job_id)
    assert job is not None and job.state == JobState.NEEDS_AGENT and job.worktree
    worktree = Path(job.worktree)
    (worktree / "app.py").write_text("both\n")
    git(worktree, "add", "app.py")
    git(worktree, "commit", "-q", "--no-edit")

    assert landing.resume(job_id, agent="resolver", token=handed.token) == JobState.SUCCEEDED


def test_a_job_stopped_again_after_each_resume_counts_resumes_under_the_key_tm_wave_reads(
    tmp_path: Path,
) -> None:
    workflow = Path(__file__).resolve().parents[2] / "plugin" / "workflows" / "tm-wave.js"
    assert "j['result.resumed']" in workflow.read_text(encoding="utf-8")
    claims, landing = estate_with(tmp_path, TRUE)
    reviewed_task(claims)
    job_id, _ = land_ungated(claims, landing)

    for resumes in (1, 2):
        claims.start("T1", "agent", "s1")
        assert landing.resume(job_id) == JobState.NEEDS_AGENT
        job = claims.jobs.get(job_id)
        assert job is not None and job.result["resumed"] == resumes


def plan_on_spec_branch(claims: Claims) -> Path:
    """Plan P, approved, whose two tasks' granular commits sit on tm/P, over the spec branch tm/S."""
    api = claims.root / "api"
    add(claims, "S", NodeKind.SPEC)
    add(
        claims,
        "P",
        NodeKind.PLAN,
        parent="S",
        merge=Merge.PARENT,
        review=True,
        fix=True,
        status=Status.REVIEWED,
        outcome=Outcome.APPROVE,
        review_cycles=1,
    )
    add(claims, "A", parent="P", merge=Merge.PARENT, status=Status.COMPLETED)
    add(claims, "B", parent="P", merge=Merge.PARENT, status=Status.COMPLETED)
    branch_at(api, "tm/S")
    branch_at(api, "tm/P", "tm/S")
    on_branch(api, "tm/P", "a.py", "a\n")
    on_branch(api, "tm/P", "b.py", "b\n")
    return api


def test_a_plan_lands_on_its_spec_branch_as_one_commit(tmp_path: Path) -> None:
    claims, landing = estate_with(tmp_path, TRUE)
    api = plan_on_spec_branch(claims)
    before = git(api, "rev-parse", "tm/S")
    merged_tree = git(api, "merge-tree", "--write-tree", before, "tm/P")

    assert land(claims, landing, "P")[1] == JobState.SUCCEEDED

    assert git(api, "rev-list", "--count", f"{before}..tm/S") == "1"
    assert git(api, "rev-list", "--parents", "-n1", "tm/S").split()[1:] == [before]
    assert git(api, "rev-parse", "tm/S^{tree}") == merged_tree
    assert git(api, "log", "-1", "--format=%s", "tm/S") == "P: P"
    assert git(api, "rev-list", "--count", f"{before}..tm/P") == "2"


def test_a_spec_lands_on_its_target_as_one_commit_per_repository(tmp_path: Path) -> None:
    claims = make_estate(tmp_path, repos=("r1", "r2"), config=gated("r1", "r2"))
    landing = attach_landing(claims)
    add(
        claims,
        "S",
        NodeKind.SPEC,
        review=True,
        fix=True,
        status=Status.REVIEWED,
        outcome=Outcome.APPROVE,
        review_cycles=1,
    )
    claims.ops.update_node(
        "S", frontmatter_set={"commit": {"r1": "feat(S): one", "r2": "fix(S): two"}}
    )
    before = {}
    for repo in ("r1", "r2"):
        add(claims, f"T-{repo}", parent="S", repo=repo, merge=Merge.PARENT, status=Status.COMPLETED)
        on_branch(claims.root / repo, "tm/S", "f.py", f"{repo}\n")
        before[repo] = git(claims.root / repo, "rev-parse", "origin/main")

    assert land(claims, landing, "S")[1] == JobState.SUCCEEDED

    for repo, subject in (("r1", "feat(S): one"), ("r2", "fix(S): two")):
        clone = claims.root / repo
        assert git(clone, "rev-list", "--count", f"{before[repo]}..origin/main") == "1"
        assert git(clone, "log", "-1", "--format=%s", "origin/main") == subject
        assert git(clone, "log", "-1", "--format=%b", "origin/main") == ""


def test_a_spec_commit_string_and_fallback_subjects(tmp_path: Path) -> None:
    claims, _ = estate_with(tmp_path, TRUE)
    add(claims, "S", NodeKind.SPEC)
    add(claims, "P", NodeKind.PLAN, parent="S")

    fallback = claims.squash_subject("S", "api")
    claims.ops.update_node("S", frontmatter_set={"commit": "feat(S): everywhere"})
    everywhere = (claims.squash_subject("S", "api"), claims.squash_subject("S", "web"))
    claims.ops.update_node("S", frontmatter_set={"commit": {"web": "fix(S): web"}})

    assert fallback == "S: S"
    assert everywhere == ("feat(S): everywhere", "feat(S): everywhere")
    assert claims.squash_subject("S", "api") == "S: S"
    assert claims.squash_subject("P", "api") == "P: P"


def test_a_resolved_container_conflict_still_lands_one_commit(tmp_path: Path) -> None:
    claims, landing = estate_with(tmp_path, TRUE)
    api = plan_on_spec_branch(claims)
    on_branch(api, "tm/P", "app.py", "plan\n")
    on_branch(api, "tm/S", "app.py", "spec\n")
    before = git(api, "rev-parse", "tm/S")

    job_id, state = land(claims, landing, "P")
    assert state == JobState.NEEDS_AGENT
    handed = claims.start("P", "resolver", "s2")
    assert handed.worktree is not None
    worktree = Path(handed.worktree)
    (worktree / "app.py").write_text("both\n")
    git(worktree, "add", "app.py")
    git(worktree, "commit", "-q", "--no-edit")

    assert landing.resume(job_id) == JobState.SUCCEEDED

    assert git(api, "rev-list", "--count", f"{before}..tm/S") == "1"
    assert git(api, "rev-list", "--parents", "-n1", "tm/S").split()[1:] == [before]
    assert git(api, "show", "tm/S:app.py") == "both"
    assert git(api, "show", "tm/S:a.py") == "a"


MOVE_TARGET = """
import pathlib, subprocess, sys
api, flag = sys.argv[1], pathlib.Path(sys.argv[2])
if not flag.exists():
    flag.touch()
    def git(*args):
        return subprocess.run(["git", "-C", api, *args], check=True, capture_output=True, text=True).stdout.strip()
    tree = git("rev-parse", "refs/heads/tm/S^{tree}")
    moved = git("commit-tree", tree, "-p", "refs/heads/tm/S", "-m", "moved")
    git("update-ref", "refs/heads/tm/S", moved)
"""


def test_a_target_that_moved_during_the_gate_gains_one_commit_on_top(tmp_path: Path) -> None:
    script = tmp_path / "move_target.py"
    script.write_text(MOVE_TARGET)
    command = f"{sys.executable} {script} {tmp_path / 'estate' / 'api'} {tmp_path / 'moved'}"
    repos = {
        "api": RepoConfig(
            push=True, gates={"main": TRUE, "parent": Gate(command=command, junit=None, timeout=60)}
        )
    }
    claims = make_estate(tmp_path, config=ProjectConfig(repos=repos))
    landing = attach_landing(claims)
    api = plan_on_spec_branch(claims)

    assert land(claims, landing, "P")[1] == JobState.SUCCEEDED

    head = git(api, "rev-parse", "tm/S")
    parent = git(api, "rev-parse", "tm/S^")
    assert git(api, "log", "-1", "--format=%s", parent) == "moved"
    assert git(api, "rev-list", "--count", f"{parent}..{head}") == "1"
    assert git(api, "show", "tm/S:b.py") == "b"


def test_a_task_landing_at_the_top_stays_a_no_ff_merge(tmp_path: Path) -> None:
    claims, landing = estate_with(tmp_path, TRUE)
    api = claims.root / "api"
    reviewed_task(claims)
    before = git(api, "rev-parse", "origin/main")

    assert land(claims, landing)[1] == JobState.SUCCEEDED

    assert len(git(api, "rev-list", "--parents", "-n1", "origin/main").split()) == 3
    assert git(api, "rev-list", "--count", f"{before}..origin/main") == "2"


def test_a_container_landing_killed_after_its_move_is_not_squashed_a_second_time(
    tmp_path: Path,
) -> None:
    claims, landing = estate_with(tmp_path, TRUE)
    api = plan_on_spec_branch(claims)
    before = git(api, "rev-parse", "tm/S")
    first = claims.start("P", "merger", "s1", ttl=1)
    assert first.job is not None
    dead = claims.jobs.get(first.job)
    assert dead is not None
    tree = git(api, "merge-tree", "--write-tree", before, "tm/P")
    squash = git(api, "commit-tree", tree, "-p", before, "-m", "P: P")
    git(api, "update-ref", "refs/heads/tm/S", squash, before)
    dead.result.update(squash=squash, tip=git(api, "rev-parse", "tm/P"))
    assert claims.jobs.update(dead)
    time.sleep(1.5)
    assert claims.sweep() == ["P"]

    second = claims.start("P", "merger", "s1")
    assert second.job is not None
    assert landing.run(second.job) == JobState.SUCCEEDED

    assert git(api, "rev-parse", "tm/S") == squash


def local_spec(tmp_path: Path, push: bool = False) -> tuple[Claims, Landing, Path]:
    repos = {"api": RepoConfig(push=push, gates={"main": TRUE})}
    claims = make_estate(tmp_path, config=ProjectConfig(repos=repos))
    landing = attach_landing(claims)
    add(
        claims,
        "S",
        NodeKind.SPEC,
        review=True,
        fix=True,
        status=Status.REVIEWED,
        outcome=Outcome.APPROVE,
        review_cycles=1,
    )
    add(claims, "T", parent="S", merge=Merge.PARENT, status=Status.COMPLETED)
    api = claims.root / "api"
    on_branch(api, "tm/S", "f.py", "f\n")
    return claims, landing, api


def fix_landing(claims: Claims, landing: Landing, api: Path, name: str = "fix.py") -> JobState:
    """The spec's rejected review is fixed on a branch cut from its target, which lands again."""
    target = "origin/main" if claims.config.repos["api"].push else "main"
    n = 1
    while git(api, "branch", "--list", f"tm/S@{n}"):
        n += 1
    git(api, "branch", "-m", "tm/S", f"tm/S@{n}")
    branch_at(api, "tm/S", target)
    on_branch(api, "tm/S", name, f"{name}\n")
    claims.nodes.save_node(
        stored(claims, "S").model_copy(update={"status": Status.FIXED, "fix_for": Outcome.REJECT})
    )
    return land(claims, landing, "S")[1]


def test_a_spec_fix_folds_into_one_commit_with_a_backup(tmp_path: Path) -> None:
    claims, landing, api = local_spec(tmp_path)
    base = git(api, "rev-parse", "main")
    assert land(claims, landing, "S")[1] == JobState.SUCCEEDED
    first = git(api, "rev-parse", "main")

    assert fix_landing(claims, landing, api) == JobState.SUCCEEDED

    assert git(api, "rev-list", "--count", f"{base}..main") == "1"
    assert git(api, "rev-list", "--parents", "-n1", "main").split()[1:] == [base]
    assert git(api, "show", "main:f.py") == "f"
    assert git(api, "show", "main:fix.py") == "fix.py"
    assert git(api, "rev-parse", "backup/S") == first
    assert git(api, "log", "-1", "--format=%s", "main") == "S: S"
    assert "fold skipped" not in section(claims, "S", "merge")
    assert "previous head kept at backup/S" in section(claims, "S", "merge")


def test_a_second_fold_takes_the_next_backup_name(tmp_path: Path) -> None:
    claims, landing, api = local_spec(tmp_path)
    base = git(api, "rev-parse", "main")
    assert land(claims, landing, "S")[1] == JobState.SUCCEEDED
    assert fix_landing(claims, landing, api) == JobState.SUCCEEDED
    second = git(api, "rev-parse", "main")

    assert fix_landing(claims, landing, api, "again.py") == JobState.SUCCEEDED

    assert git(api, "rev-list", "--count", f"{base}..main") == "1"
    assert git(api, "rev-parse", "backup/S-r2") == second
    assert git(api, "show", "main:fix.py") == "fix.py"
    assert git(api, "show", "main:again.py") == "again.py"


def test_the_fold_skips_when_another_branch_holds_the_range(tmp_path: Path) -> None:
    claims, landing, api = local_spec(tmp_path)
    base = git(api, "rev-parse", "main")
    assert land(claims, landing, "S")[1] == JobState.SUCCEEDED
    git(api, "branch", "other", "main")

    assert fix_landing(claims, landing, api) == JobState.SUCCEEDED

    assert git(api, "rev-list", "--count", f"{base}..main") == "2"
    note = section(claims, "S", "merge")
    assert "fold skipped" in note and "branch other" in note
    assert not git(api, "branch", "--list", "backup/*")


def test_the_fold_skips_a_range_the_owner_pushed(tmp_path: Path) -> None:
    claims, landing, api = local_spec(tmp_path)
    base = git(api, "rev-parse", "main")
    assert land(claims, landing, "S")[1] == JobState.SUCCEEDED
    git(api, "push", "-q", "origin", "main")

    assert fix_landing(claims, landing, api) == JobState.SUCCEEDED

    assert git(api, "rev-list", "--count", f"{base}..main") == "2"
    assert "fold skipped" in section(claims, "S", "merge")


def test_the_fold_skips_when_push_is_on(tmp_path: Path) -> None:
    claims, landing, api = local_spec(tmp_path, push=True)
    base = git(api, "rev-parse", "origin/main")
    assert land(claims, landing, "S")[1] == JobState.SUCCEEDED

    assert fix_landing(claims, landing, api) == JobState.SUCCEEDED

    assert git(api, "rev-list", "--count", f"{base}..origin/main") == "2"
    assert "fold skipped" in section(claims, "S", "merge")


def test_the_fold_skips_foreign_commits(tmp_path: Path) -> None:
    claims, landing, api = local_spec(tmp_path)
    base = git(api, "rev-parse", "main")
    assert land(claims, landing, "S")[1] == JobState.SUCCEEDED
    commit(api, "x.py", "x\n", "foreign")

    assert fix_landing(claims, landing, api) == JobState.SUCCEEDED

    assert git(api, "rev-list", "--count", f"{base}..main") == "3"
    assert "fold skipped" in section(claims, "S", "merge")
    assert "not this spec's" in section(claims, "S", "merge")


def add_spec_on_main(claims: Claims, api: Path, spec_id: str, path: str) -> None:
    add(
        claims,
        spec_id,
        NodeKind.SPEC,
        review=True,
        fix=True,
        status=Status.IMPLEMENTED,
    )
    add(claims, f"T-{spec_id}", parent=spec_id, merge=Merge.PARENT, status=Status.COMPLETED)
    on_branch(api, f"tm/{spec_id}", path, f"{path}\n")


def reviewed_after_landing(tmp_path: Path) -> tuple[Claims, Landing, Path]:
    """A local spec whose review comes after its landing."""
    repos = {"api": RepoConfig(push=False, gates={"main": TRUE})}
    claims = make_estate(tmp_path, config=ProjectConfig(repos=repos))
    api = claims.root / "api"
    add_spec_on_main(claims, api, "S", "f.py")
    return claims, attach_landing(claims), api


def test_a_review_of_a_landed_spec_names_the_commit_its_landing_squashed_to(
    tmp_path: Path,
) -> None:
    claims, landing, api = reviewed_after_landing(tmp_path)
    assert land(claims, landing, "S")[1] == JobState.SUCCEEDED
    squash = git(api, "rev-parse", "main")

    review = claims.start("S", "reviewer", "s2")

    assert review.action == Action.REVIEW
    assert review.squashes == {"api": squash}


def test_a_later_landing_on_the_same_target_leaves_a_specs_review_on_its_own_squash(
    tmp_path: Path,
) -> None:
    claims, landing, api = reviewed_after_landing(tmp_path)
    assert land(claims, landing, "S")[1] == JobState.SUCCEEDED
    squash = git(api, "rev-parse", "main")
    add_spec_on_main(claims, api, "S2", "g.py")
    assert land(claims, landing, "S2")[1] == JobState.SUCCEEDED
    assert git(api, "rev-parse", "main") != squash

    review = claims.start("S", "reviewer", "s2")

    assert review.action == Action.REVIEW
    assert review.squashes == {"api": squash}


def test_a_folded_specs_review_names_the_folded_commit(tmp_path: Path) -> None:
    claims, landing, api = reviewed_after_landing(tmp_path)
    assert land(claims, landing, "S")[1] == JobState.SUCCEEDED
    assert fix_landing(claims, landing, api) == JobState.SUCCEEDED
    folded = git(api, "rev-parse", "main")

    review = claims.start("S", "reviewer", "s2")

    assert review.squashes == {"api": folded}


def test_a_container_landed_without_a_squash_has_no_entry_in_its_reviews_squashes(
    tmp_path: Path,
) -> None:
    claims, landing, _ = reviewed_after_landing(tmp_path)
    land(claims, landing, "S")
    merge = claims.nodes.get_section("S", "merge")
    assert merge is not None
    kept = [ln for ln in merge.content.splitlines() if not ln.startswith("squash: ")]
    claims.ops.set_section("S", "merge", "\n".join(kept), merge.header)

    review = claims.start("S", "reviewer", "s2")

    assert review.action == Action.REVIEW
    assert review.squashes == {}


def test_a_review_reads_the_squash_from_the_merge_note_and_not_from_the_jobs(
    tmp_path: Path,
) -> None:
    claims, landing, api = reviewed_after_landing(tmp_path)
    job_id, _ = land(claims, landing, "S")
    squash = git(api, "rev-parse", "main")
    job = claims.jobs.get(job_id)
    assert job is not None
    job.result.pop("squash")
    assert claims.jobs.set_state(job)

    review = claims.start("S", "reviewer", "s2")

    assert f"squash: api {squash}\n" in section(claims, "S", "merge") + "\n"
    assert review.squashes == {"api": squash}


def test_a_landing_retried_past_its_move_records_the_squash_it_found_on_the_target(
    tmp_path: Path,
) -> None:
    claims, landing, api = reviewed_after_landing(tmp_path)
    before = git(api, "rev-parse", "main")
    first = claims.start("S", "merger", "s1", ttl=1)
    assert first.job is not None
    dead = claims.jobs.get(first.job)
    assert dead is not None
    tree = git(api, "merge-tree", "--write-tree", before, "tm/S")
    squash = git(api, "commit-tree", tree, "-p", before, "-m", "S: S")
    git(api, "update-ref", "refs/heads/main", squash, before)
    dead.result.update(squash=squash, tip=git(api, "rev-parse", "tm/S"))
    assert claims.jobs.update(dead)
    time.sleep(1.5)
    assert claims.sweep() == ["S"]
    second = claims.start("S", "merger", "s1")
    assert second.job is not None
    assert landing.run(second.job) == JobState.SUCCEEDED

    review = claims.start("S", "reviewer", "s2")

    assert review.action == Action.REVIEW
    assert review.squashes == {"api": squash}


def test_a_job_whose_push_failed_leaves_no_squash_line_for_a_commit_that_is_not_on_the_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    claims, landing, api = reviewed_after_landing(tmp_path)
    real = landing_git.push
    tried: list[str] = []

    def refused_once(*args: Any, **kwargs: Any) -> Any:
        if tried:
            return real(*args, **kwargs)
        tried.append(git(args[0], "rev-parse", "HEAD"))
        return subprocess.CompletedProcess([], 1, "", "refused")

    monkeypatch.setattr(landing_git, "push", refused_once)
    assert land(claims, landing, "S")[1] == JobState.SUCCEEDED

    recorded = claims.recorded_squashes("S")
    assert recorded == {"api": git(api, "rev-parse", "main")}
    assert tried[0] not in recorded.values()


def test_a_reset_to_landed_is_refused_while_the_containers_branch_holds_commits_its_squash_lacks(
    tmp_path: Path,
) -> None:
    claims, landing, api = reviewed_after_landing(tmp_path)
    assert land(claims, landing, "S")[1] == JobState.SUCCEEDED
    on_branch(api, "tm/S", "late.py", "late\n", base="tm/S")

    with pytest.raises(OperationError, match="did not take in"):
        claims.reset("S", Status.LANDED, "repair")

    assert stored(claims, "S").status == Status.LANDED
    git(api, "branch", "-f", "tm/S", "tm/S~1")
    claims.reset("S", Status.COMPLETED, "repair")
    assert stored(claims, "S").status == Status.COMPLETED


def test_a_reset_of_a_container_with_no_recorded_squash_proves_its_branch_is_on_the_target(
    tmp_path: Path,
) -> None:
    claims, landing, api = reviewed_after_landing(tmp_path)
    assert land(claims, landing, "S")[1] == JobState.SUCCEEDED
    merge = claims.nodes.get_section("S", "merge")
    assert merge is not None
    kept = [ln for ln in merge.content.splitlines() if not ln.startswith("squash: ")]
    claims.ops.set_section("S", "merge", "\n".join(kept), merge.header)

    with pytest.raises(OperationError, match="is not on main"):
        claims.reset("S", Status.COMPLETED, "repair")

    git(api, "branch", "-f", "tm/S", "main")
    claims.reset("S", Status.COMPLETED, "repair")
    assert stored(claims, "S").status == Status.COMPLETED


def test_a_reset_of_a_squashed_container_runs_its_checks_on_the_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    claims, landing, _ = reviewed_after_landing(tmp_path)
    assert land(claims, landing, "S")[1] == JobState.SUCCEEDED
    read: list[str] = []

    def red(node_id: str, ref: str, repo: str) -> tuple[bool, str, None]:
        read.append(ref)
        return False, "one row red", None

    monkeypatch.setattr(claims, "verify", red)

    with pytest.raises(OperationError, match="verifications red on main"):
        claims.reset("S", Status.COMPLETED, "repair")

    assert read == ["main"]
    assert stored(claims, "S").status == Status.LANDED


def fold_after_a_lost_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, move_backup: bool
) -> tuple[Claims, Path]:
    claims, landing, api = local_spec(tmp_path)
    assert land(claims, landing, "S")[1] == JobState.SUCCEEDED
    real = landing_git.push
    lost: list[bool] = []

    def lost_once(*args: Any, **kwargs: Any) -> Any:
        if lost:
            return real(*args, **kwargs)
        lost.append(True)
        git(api, "branch", "other", "main")
        if move_backup:
            git(api, "branch", "-f", "backup/S", "main~1")
        return subprocess.CompletedProcess([], 1, "", "stale info")

    monkeypatch.setattr(landing_git, "push", lost_once)
    assert fix_landing(claims, landing, api) == JobState.SUCCEEDED
    return claims, api


def test_a_fold_refused_after_its_backup_was_cut_deletes_that_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    claims, api = fold_after_a_lost_lease(tmp_path, monkeypatch, move_backup=False)

    assert not git(api, "branch", "--list", "backup/*")
    assert "fold skipped" in section(claims, "S", "merge")


def test_a_fold_refused_after_its_backup_moved_names_the_backup_in_the_note(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    claims, api = fold_after_a_lost_lease(tmp_path, monkeypatch, move_backup=True)

    assert git(api, "branch", "--list", "backup/S")
    assert "backup/S holds the previous head" in section(claims, "S", "merge")
