import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from lifecycle_estate import (
    add,
    attach_landing,
    branch_at,
    gate_runs,
    git,
    junit_gate,
    make_estate,
    on_branch,
    push_main,
    section,
    stored,
)

from taskmanager.core.enums import NodeKind, VerificationType
from taskmanager.core.models import Condition, NodeVerification
from taskmanager.core.status import Action, ConditionStage, JobState, Merge, Outcome, Status
from taskmanager.engine.claims import Claims
from taskmanager.engine.config import Gate, ProjectConfig, RepoConfig
from taskmanager.engine.landing import Landing
from taskmanager.engine.operations import OperationError

TRUE = Gate(command="true", junit=None, timeout=60)


def estate_with(tmp_path: Path, gate: Gate | None, **config: object) -> tuple[Claims, Landing]:
    repos = {"api": RepoConfig(gates={"main": gate})} if gate is not None else {}
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


def test_the_module_entry_point_runs_a_landing_as_a_detached_process(tmp_path: Path) -> None:
    claims = make_estate(
        tmp_path, config=ProjectConfig(repos={"api": RepoConfig(gates={"main": TRUE})})
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
    claims, landing = estate_with(tmp_path, None)
    reviewed_task(claims)

    job_id, state = land(claims, landing)

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
    claims, landing = estate_with(tmp_path, None)
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
    claims, landing = estate_with(tmp_path, None)
    parent_landing(claims, fix=fix, outcome=outcome)
    verification(claims, "T1", "api/missing.py")

    assert land(claims, landing)[1] == state
    assert "FAIL" in section(claims, "T1", "merge")


def test_a_parent_branch_that_moves_before_the_swap_is_merged_in_and_gated_again(
    tmp_path: Path,
) -> None:
    claims, landing = estate_with(tmp_path, None)
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
    claims, landing = estate_with(tmp_path, None)
    claims.config.repos["api"] = RepoConfig(gates={"main": release_from_the_gate(claims.root)})
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
            repos={"api": RepoConfig(gates={"main": Gate(command="sleep 3", timeout=60)})}
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
