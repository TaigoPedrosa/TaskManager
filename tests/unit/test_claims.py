import threading
import time
from pathlib import Path
from typing import Any

import pytest
from lifecycle_estate import (
    add,
    branch_at,
    commit,
    gated,
    git,
    make_estate,
    on_branch,
    push_main,
    section,
    stored,
)

from taskmanager.core.enums import NodeKind, RelationType, VerificationType
from taskmanager.core.models import Condition, Job, Node, NodeRelation, NodeVerification
from taskmanager.core.status import (
    Action,
    CheckResult,
    ConditionStage,
    FindingSource,
    FindingState,
    JobKind,
    JobState,
    Merge,
    Outcome,
    Severity,
    Status,
)
from taskmanager.engine.claims import Blocker, ClaimResult, Claims
from taskmanager.engine.config import Gate, ProjectConfig, RepoConfig
from taskmanager.engine.operations import OperationError
from taskmanager.engine.snapshot import DisplayView


class FakeLanding:
    def __init__(self) -> None:
        self.started: list[str] = []

    def start_land(self, node_id: str) -> str:
        self.started.append(node_id)
        return "job-1"

    def start_sync(self, node_id: str, pairs: list[tuple[str, str, str]]) -> str:
        raise AssertionError("no sync is expected here")


def decisions_blocking(claims: Claims, node_id: str) -> list[str]:
    return [
        dep
        for dep in claims.nodes.get_dependencies(node_id)
        if stored(claims, dep).kind == NodeKind.DECISION
    ]


def test_a_containers_locked_files_are_the_same_set_the_display_builds_from_its_snapshot(
    tmp_path: Path,
) -> None:
    """A set-aside child's files are excluded from both: one walk, read two ways."""
    claims = make_estate(tmp_path, config=gated("api"))
    add(claims, "P1", kind=NodeKind.PLAN)
    add(claims, "T1", parent="P1", files=["api/a.py"])
    add(claims, "T2", parent="P1", files=["api/b.py"], status=Status.SUPERSEDED)
    node = stored(claims, "P1")
    snap = claims.snapshots.build()

    locked = claims._locked_files(node, Action.FIX, snap)

    assert locked == ["api:api/a.py"]
    assert locked == claims.snapshots.lock_set("P1", snap)


def test_an_implement_claim_cuts_the_branch_from_origin_main_and_locks_declared_files(
    tmp_path: Path,
) -> None:
    claims = make_estate(tmp_path, config=gated("api"))
    add(claims, "T1", files=["api/app.py"], models=["claude-sonnet-4", "claude-opus-4"])

    result = claims.start("T1", "agent-1", "s1")

    assert result.action == Action.IMPLEMENT
    assert result.model == "claude-sonnet-4"
    assert (result.branch, result.base, result.repos) == ("tm/T1", "main", ["api"])
    assert result.worktree is not None
    assert result.worktrees == {"api": result.worktree}
    worktree = Path(result.worktree)
    assert git(worktree, "rev-parse", "--abbrev-ref", "HEAD") == "tm/T1"
    assert git(worktree, "rev-parse", "HEAD") == git(
        claims.root / "api", "rev-parse", "origin/main"
    )
    node = stored(claims, "T1")
    assert (node.status, node.claimed_from) == (Status.IMPLEMENTING, Status.READY)
    lease = claims.runtime.get_lease("T1")
    assert lease is not None
    assert (lease.action, lease.ttl_seconds, lease.model) == (
        Action.IMPLEMENT,
        claims.ttl_for(Action.IMPLEMENT),
        "claude-sonnet-4",
    )
    assert claims.runtime.get_conflicting_tasks(["api:api/app.py"])


def test_a_claim_cuts_its_worktree_under_the_directory_the_caller_names(tmp_path: Path) -> None:
    claims = make_estate(tmp_path, config=gated("api"))
    add(claims, "T1")

    result = claims.start("T1", "agent-1", "s1", worktree_dir=tmp_path / "wave")

    assert result.worktree is not None
    assert Path(result.worktree).parent == tmp_path / "wave"
    assert git(Path(result.worktree), "rev-parse", "--abbrev-ref", "HEAD") == "tm/T1"


def test_an_implement_claim_resumes_an_existing_branch_and_a_fix_reuses_its_worktree(
    tmp_path: Path,
) -> None:
    claims = make_estate(tmp_path, config=gated("api"))
    api = claims.root / "api"
    earlier = on_branch(api, "tm/T1", "a.py", "a = 1\n")
    add(claims, "T1")

    first = claims.start("T1", "implementer", "s1")

    assert first.worktree is not None
    assert git(Path(first.worktree), "rev-parse", "HEAD") == earlier
    assert git(api, "rev-parse", "tm/T1") == earlier
    claims.complete("T1")
    claims.start("T1", "reviewer", "s1")
    claims.add_finding("T1", Severity.BLOCKING, "a.py:1", "a.py needs a docstring")
    claims.review("T1", approve=False)
    fix = claims.start("T1", "fixer", "s1")
    assert (fix.action, fix.worktree) == (Action.FIX, first.worktree)
    assert git(Path(fix.worktree), "rev-parse", "HEAD") == earlier


@pytest.mark.parametrize(
    ("review_cycles", "model"),
    [(1, "claude-opus-5-5"), (2, "claude-opus-5-5"), (3, "claude-fable-5-1")],
)
def test_a_container_fix_is_routed_by_the_one_based_review_round_it_answers(
    tmp_path: Path, review_cycles: int, model: str
) -> None:
    claims = make_estate(tmp_path, config=gated("api"))
    add(
        claims,
        "P",
        NodeKind.PLAN,
        review=True,
        fix=True,
        status=Status.REVIEWED,
        outcome=Outcome.REJECT,
        review_cycles=review_cycles,
        models=["claude-opus-5-5", "claude-fable-5-1"],
    )

    result = claims.start("P", "fixer", "s1")

    assert (result.action, result.model) == (Action.FIX, model)
    lease = claims.runtime.get_lease("P")
    assert lease is not None and lease.model == model


def test_closing_a_step_with_an_agent_is_refused_unless_that_agent_holds_the_lease(
    tmp_path: Path,
) -> None:
    claims = make_estate(tmp_path, config=gated("api"))
    add(claims, "T1")
    add(claims, "T2", status=Status.IMPLEMENTED)
    claims.start("T1", "implementer", "s1")
    claims.start("T2", "reviewer", "s1")

    for close in (
        lambda: claims.complete("T1", agent="intruder"),
        lambda: claims.release("T1", agent="intruder"),
        lambda: claims.review("T2", approve=True, clean=True, agent="intruder"),
    ):
        with pytest.raises(OperationError, match="holds no live lease") as refused:
            close()
        assert refused.value.status_code == 409

    assert stored(claims, "T1").status == Status.IMPLEMENTING
    assert stored(claims, "T2").status == Status.REVIEWING
    assert claims.complete("T1", agent="implementer") == Status.IMPLEMENTED
    assert claims.review("T2", approve=True, clean=True, agent="reviewer") == Status.REVIEWED


def test_a_child_landing_on_its_parent_is_cut_from_the_parent_branch(tmp_path: Path) -> None:
    claims = make_estate(tmp_path, config=gated("api"))
    api = claims.root / "api"
    add(claims, "P", NodeKind.PLAN)
    add(claims, "T1", parent="P", merge=Merge.PARENT)

    result = claims.start("T1", "agent-1", "s1")

    assert result.base == "tm/P"
    assert git(api, "rev-parse", "tm/P") == git(api, "rev-parse", "origin/main")
    assert result.worktree is not None
    assert git(Path(result.worktree), "rev-parse", "HEAD") == git(api, "rev-parse", "tm/P")


def test_a_claim_on_an_existing_parent_branch_never_reads_the_remote_from_git(
    tmp_path: Path,
) -> None:
    """A child landing on its parent's branch never fetches, so a remote the clone lacks
    leaves its claim, and the worktree it cuts, standing."""
    config = ProjectConfig(
        repos={"api": RepoConfig(remote="gone", gates={"main": Gate(command="true")})}
    )
    claims = make_estate(tmp_path, config=config)
    api = claims.root / "api"
    git(api, "branch", "tm/P", "origin/main")
    add(claims, "P", NodeKind.PLAN)
    add(claims, "T1", parent="P", merge=Merge.PARENT)

    result = claims.start("T1", "agent-1", "s1")

    assert (result.action, result.base, result.remotes) == (
        Action.IMPLEMENT,
        "tm/P",
        {"api": "gone"},
    )
    assert result.worktree is not None and Path(result.worktree).is_dir()


@pytest.mark.parametrize(
    ("setup", "reason"),
    [
        ("edge", "waits on D"),
        ("decision", "awaiting decision"),
        ("condition", "condition unmet: flag"),
        ("lock", "declared files locked"),
        ("no_repo", "no target_repo"),
        ("completed", "has no next action"),
    ],
)
def test_a_refused_claim_answers_blocked_and_writes_nothing(
    tmp_path: Path, setup: str, reason: str
) -> None:
    claims = make_estate(tmp_path, config=gated("api"))
    add(
        claims,
        "T1",
        files=["api/app.py"],
        repo=None if setup == "no_repo" else "api",
        status=Status.COMPLETED if setup == "completed" else Status.READY,
    )
    if setup == "edge":
        add(claims, "D")
        claims.nodes.add_relation(
            NodeRelation(source_id="T1", target_id="D", relation_type=RelationType.DEPENDS_ON)
        )
    if setup == "decision":
        claims.ops.add_decision("Which way?", blocks=["T1"])
    if setup == "condition":
        claims.nodes.add_condition(
            Condition(
                node_id="T1",
                idx=0,
                needs="flag",
                command=f"test -f {tmp_path / 'flag'}",
                stage=ConditionStage.CLAIM,
            )
        )
    if setup == "lock":
        add(claims, "T2", files=["api/app.py"])
        assert claims.start("T2", "agent-2", "s2").action == Action.IMPLEMENT
    before = stored(claims, "T1")

    result = claims.start("T1", "agent-1", "s1")

    assert result.action == Action.BLOCKED
    assert reason in (result.reason or "")
    assert stored(claims, "T1") == before
    assert claims.runtime.get_lease("T1") is None


def test_a_completed_node_with_an_unmet_condition_reports_the_condition_not_no_next_action(
    tmp_path: Path,
) -> None:
    """The condition check runs ahead of "has no next action", same as every other rule here:
    a node with nothing left to do but an unmet condition still surfaces the condition."""
    claims = make_estate(tmp_path, config=gated("api"))
    add(claims, "T1", files=["api/app.py"], status=Status.COMPLETED)
    claims.nodes.add_condition(
        Condition(
            node_id="T1",
            idx=0,
            needs="flag",
            command=f"test -f {tmp_path / 'flag'}",
            stage=ConditionStage.CLAIM,
        )
    )

    result = claims.start("T1", "agent-1", "s1")

    assert result.action == Action.BLOCKED
    assert "condition unmet: flag" in (result.reason or "")


def test_two_sessions_claiming_at_once_leave_exactly_one_lease(tmp_path: Path) -> None:
    claims = make_estate(tmp_path, config=gated("api"))
    add(claims, "T1")
    barrier = threading.Barrier(2)
    results: list[ClaimResult] = []

    def claim(agent: str) -> None:
        barrier.wait()
        results.append(claims.start("T1", agent, agent))

    threads = [threading.Thread(target=claim, args=(agent,)) for agent in ("a", "b")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(r.action for r in results) == [Action.BLOCKED, Action.IMPLEMENT]
    winner = next(r for r in results if r.action == Action.IMPLEMENT)
    lease = claims.runtime.get_lease("T1")
    assert lease is not None and winner.worktree is not None
    assert stored(claims, "T1").status == Status.IMPLEMENTING


@pytest.mark.parametrize(
    ("configured", "action", "ttl"),
    [
        (900, Action.IMPLEMENT, 900),
        (900, Action.REVIEW, 3600),
        ({"review": 60}, Action.REVIEW, 60),
        ({"review": 60}, Action.IMPLEMENT, 10800),
    ],
)
def test_lease_ttl_reads_a_per_action_map_or_a_scalar_implement_default(
    tmp_path: Path, configured: int | dict[str, int], action: Action, ttl: int
) -> None:
    claims = make_estate(tmp_path, config=ProjectConfig(lease_ttl=configured))
    assert claims.ttl_for(action) == ttl


def test_complete_closes_implement_and_releases_the_lease(tmp_path: Path) -> None:
    claims = make_estate(tmp_path, config=gated("api"))
    add(claims, "T1")
    claims.start("T1", "agent-1", "s1")

    assert claims.complete("T1") == Status.IMPLEMENTED
    assert stored(claims, "T1").claimed_from is None
    assert claims.runtime.get_lease("T1") is None


def test_complete_refuses_a_node_that_is_not_mid_implement_or_fix(tmp_path: Path) -> None:
    claims = make_estate(tmp_path, config=gated("api"))
    add(claims, "T1")
    with pytest.raises(OperationError) as refused:
        claims.complete("T1")
    assert refused.value.status_code == 409


def test_a_review_closes_only_after_recording_a_finding_or_declaring_itself_clean(
    tmp_path: Path,
) -> None:
    claims = make_estate(tmp_path, config=gated("api"))
    add(claims, "T1", status=Status.IMPLEMENTED)

    result = claims.start("T1", "reviewer", "s1")
    assert (result.action, result.model, result.worktree) == (
        Action.REVIEW,
        "claude-sonnet-5-5",
        None,
    )
    with pytest.raises(OperationError, match="no finding on T1 was recorded") as refused:
        claims.review("T1", approve=True)
    assert refused.value.status_code == 409

    assert claims.review("T1", approve=True, clean=True) == Status.REVIEWED
    node = stored(claims, "T1")
    assert (node.outcome, node.verdict, node.review_cycles) == (Outcome.APPROVE, "nothing open", 1)


def test_a_clean_review_is_refused_once_a_finding_was_recorded(tmp_path: Path) -> None:
    claims = make_estate(tmp_path, config=gated("api"))
    add(claims, "T1", status=Status.IMPLEMENTED)
    claims.start("T1", "reviewer", "s1")
    claims.add_finding("T1", Severity.MINOR, "a.py:3", "the name hides the unit")

    with pytest.raises(OperationError, match="drop --clean") as refused:
        claims.review("T1", approve=True, clean=True)
    assert refused.value.status_code == 409
    assert stored(claims, "T1").status == Status.REVIEWING


def test_an_approve_with_an_open_blocking_finding_is_refused(tmp_path: Path) -> None:
    claims = make_estate(tmp_path, config=gated("api"))
    add(claims, "T1", status=Status.IMPLEMENTED)
    claims.start("T1", "reviewer", "s1")
    claims.add_finding("T1", Severity.MINOR, "a.py:3", "the name hides the unit")
    claims.add_finding("T1", Severity.BLOCKING, "a.py:9", "a null id reads every tenant")

    with pytest.raises(OperationError, match="open blocking findings #2: reject it"):
        claims.review("T1", approve=True)

    assert claims.review("T1", approve=False) == Status.REVIEWED
    node = stored(claims, "T1")
    assert (node.outcome, node.verdict) == (Outcome.REJECT, "1 blocking, 1 minor open")


def test_a_reject_with_only_minor_findings_is_refused_and_the_approve_lands(
    tmp_path: Path,
) -> None:
    claims = make_estate(tmp_path, config=gated("api"))
    add(claims, "T1", status=Status.IMPLEMENTED)
    claims.start("T1", "reviewer", "s1")
    claims.add_finding("T1", Severity.MINOR, "a.py:3", "the name hides the unit")

    with pytest.raises(OperationError, match="no open blocking finding: approve it"):
        claims.review("T1", approve=False)

    assert claims.review("T1", approve=True) == Status.REVIEWED
    node = stored(claims, "T1")
    assert (node.outcome, node.verdict) == (Outcome.APPROVE, "1 minor open")
    claims.landing = FakeLanding()
    assert claims.start("T1", "lander", "s1").action == Action.MERGE


def test_a_finding_is_recorded_only_under_the_review_lease_with_where_and_what(
    tmp_path: Path,
) -> None:
    claims = make_estate(tmp_path, config=gated("api"))
    add(claims, "T1")
    add(claims, "T2", status=Status.IMPLEMENTED)
    claims.start("T1", "implementer", "s1")
    claims.start("T2", "reviewer", "s1")

    with pytest.raises(OperationError, match="IMPLEMENTING"):
        claims.add_finding("T1", Severity.BLOCKING, "a.py:1", "x")
    with pytest.raises(OperationError, match="holds no live lease"):
        claims.add_finding("T2", Severity.BLOCKING, "a.py:1", "x", agent="intruder")
    for at, breaks in (("", "x"), (" ", "x"), ("a.py:1", ""), ("a.py:1", " ")):
        with pytest.raises(OperationError, match="--at"):
            claims.add_finding("T2", Severity.BLOCKING, at, breaks)

    found = claims.add_finding("T2", Severity.BLOCKING, "a.py:1", "x", rule="scope by tenant")
    assert (found.number, found.state, found.rule) == (1, FindingState.OPEN, "scope by tenant")
    assert claims.add_finding("T2", Severity.MINOR, "a.py:2", "y").number == 2


def test_a_finding_already_filed_on_the_node_is_refused_naming_it(tmp_path: Path) -> None:
    claims = make_estate(tmp_path, config=gated("api"))
    add(claims, "T1", status=Status.IMPLEMENTED)
    claims.start("T1", "reviewer", "s1")
    claims.add_finding("T1", Severity.BLOCKING, "a.py:1", "a null id reads every tenant")

    with pytest.raises(OperationError) as refused:
        claims.add_finding("T1", Severity.MINOR, " A.py:1 ", "a null  id reads EVERY tenant")
    assert refused.value.status_code == 409
    assert str(refused.value) == "T1 already holds this finding as #1 (open): it stands as filed"

    assert claims.add_finding("T1", Severity.BLOCKING, "a.py:1", "the id is logged").number == 2
    assert claims.add_finding("T1", Severity.BLOCKING, "b.py:1", "the id is logged").number == 3
    assert len(claims.nodes.get_findings("T1")) == 3


def test_a_re_review_refiling_an_answered_finding_is_sent_to_reopen_it(tmp_path: Path) -> None:
    claims = make_estate(tmp_path, config=gated("api"))
    _sensitive_re_review(claims)

    with pytest.raises(
        OperationError,
        match=r"^T1 already holds this finding as #1 \(disputed\): `tm finding reopen T1 1` when",
    ):
        claims.add_finding("T1", Severity.BLOCKING, "a.py:1", "a null id reads every tenant")
    assert len(claims.nodes.get_findings("T1")) == 2


def _rejected_and_fixing(claims: Claims) -> Path:
    """The fix worktree of T1, rejected on one blocking and one minor finding."""
    add(claims, "T1", status=Status.IMPLEMENTED)
    claims.start("T1", "reviewer", "s1")
    claims.add_finding("T1", Severity.BLOCKING, "a.py:1", "a null id reads every tenant")
    claims.add_finding("T1", Severity.MINOR, "a.py:2", "the name hides the unit")
    claims.review("T1", approve=False)
    fix = claims.start("T1", "fixer", "s1")
    assert fix.action == Action.FIX and fix.worktree is not None
    return Path(fix.worktree)


def test_a_fix_completes_only_once_every_finding_is_closed_or_disputed(tmp_path: Path) -> None:
    claims = make_estate(tmp_path, config=gated("api"))
    fixed = commit(_rejected_and_fixing(claims), "a.py", "a = 2\n", "scope the id")

    with pytest.raises(OperationError, match="unanswered findings #1, #2") as refused:
        claims.complete("T1")
    assert refused.value.status_code == 409

    claims.answer_finding("T1", 1, FindingState.CLOSED, fixed[:8])
    with pytest.raises(OperationError, match="unanswered findings #2"):
        claims.complete("T1")
    claims.answer_finding("T1", 2, FindingState.DISPUTED, "the unit is in the type")
    assert claims.complete("T1") == Status.FIXED
    assert [(f.state, f.answer) for f in claims.nodes.get_findings("T1")] == [
        (FindingState.CLOSED, fixed),
        (FindingState.DISPUTED, "the unit is in the type"),
    ]


@pytest.mark.parametrize("ref", ["", "  ", "no-such-ref", "elsewhere", "origin/main"])
def test_a_finding_closes_only_with_a_commit_its_branch_adds(tmp_path: Path, ref: str) -> None:
    claims = make_estate(tmp_path, config=gated("api"))
    commit(_rejected_and_fixing(claims), "a.py", "a = 2\n", "scope the id")
    on_branch(claims.root / "api", "elsewhere", "b.py", "b = 1\n")

    with pytest.raises(OperationError, match="names no commit on tm/T1 that its target lacks"):
        claims.answer_finding("T1", 1, FindingState.CLOSED, ref)
    assert claims.nodes.get_findings("T1")[0].state == FindingState.OPEN


def test_a_dispute_needs_evidence_and_an_unknown_number_is_not_found(tmp_path: Path) -> None:
    claims = make_estate(tmp_path, config=gated("api"))
    _rejected_and_fixing(claims)

    for evidence in ("", " "):
        with pytest.raises(OperationError, match="carries the evidence"):
            claims.answer_finding("T1", 1, FindingState.DISPUTED, evidence)
    with pytest.raises(OperationError, match="T1 has no finding 9") as refused:
        claims.answer_finding("T1", 9, FindingState.DISPUTED, "why")
    assert refused.value.status_code == 404


def test_a_finding_is_answered_only_under_the_implement_or_fix_lease(tmp_path: Path) -> None:
    claims = make_estate(tmp_path, config=gated("api"))
    _rejected_and_fixing(claims)

    with pytest.raises(OperationError, match="holds no live lease"):
        claims.answer_finding("T1", 1, FindingState.DISPUTED, "why", agent="intruder")
    with pytest.raises(OperationError, match="FIXING"):
        claims.reopen_finding("T1", 1)


def _sensitive_re_review(claims: Claims) -> None:
    """T1 sensitive, rejected on two blocking findings its fix disputed, under its re-review."""
    add(claims, "T1", status=Status.IMPLEMENTED)
    sensitive = {"sensitive": "tenant"}
    claims.nodes.save_node(stored(claims, "T1").model_copy(update={"frontmatter": sensitive}))
    claims.start("T1", "reviewer", "s1")
    claims.add_finding("T1", Severity.BLOCKING, "a.py:1", "a null id reads every tenant")
    claims.add_finding("T1", Severity.BLOCKING, "a.py:5", "the cache is never evicted")
    claims.review("T1", approve=False)
    claims.start("T1", "fixer", "s1")
    claims.answer_finding("T1", 1, FindingState.DISPUTED, "the id is never null")
    claims.answer_finding("T1", 2, FindingState.DISPUTED, "nothing caches it")
    claims.complete("T1")
    assert claims.start("T1", "reviewer", "s1").action == Action.REVIEW


def test_a_re_review_that_reopens_a_finding_has_recorded_one(tmp_path: Path) -> None:
    claims = make_estate(tmp_path, config=gated("api"))
    _sensitive_re_review(claims)

    with pytest.raises(OperationError, match="REVIEWING"):
        claims.answer_finding("T1", 1, FindingState.DISPUTED, "why")
    with pytest.raises(OperationError, match="holds no live lease"):
        claims.reopen_finding("T1", 1, agent="intruder")
    claims.reopen_finding("T1", 1)
    with pytest.raises(OperationError, match="drop --clean"):
        claims.review("T1", approve=False, clean=True)
    assert claims.review("T1", approve=False) == Status.FAILED
    assert stored(claims, "T1").verdict == "1 blocking open"
    assert claims.nodes.get_findings("T1")[0].state == FindingState.OPEN


@pytest.mark.parametrize("write", ["add", "close", "dispute", "reopen"])
def test_a_finding_write_is_refused_when_the_lease_lapses_before_the_write_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, write: str
) -> None:
    claims = make_estate(tmp_path, config=gated("api"))
    if write == "add":
        add(claims, "T1", status=Status.IMPLEMENTED)
        claims.start("T1", "reviewer", "s1")
    elif write == "reopen":
        _sensitive_re_review(claims)
    else:
        fixed = commit(_rejected_and_fixing(claims), "a.py", "a = 2\n", "scope the id")
    lease = claims.runtime.get_lease("T1")
    assert lease is not None
    before = claims.nodes.get_findings("T1")
    checks: list[bool] = []

    def lapses_after_the_first_check(_lease: object) -> bool:
        checks.append(True)
        return len(checks) == 1

    monkeypatch.setattr(Claims, "live", staticmethod(lapses_after_the_first_check))

    with pytest.raises(OperationError, match="holds no live lease"):
        if write == "add":
            claims.add_finding("T1", Severity.MINOR, "a.py:1", "x", token=lease.token)
        elif write == "close":
            claims.answer_finding("T1", 1, FindingState.CLOSED, fixed, token=lease.token)
        elif write == "dispute":
            claims.answer_finding("T1", 1, FindingState.DISPUTED, "why", token=lease.token)
        else:
            claims.reopen_finding("T1", 1, token=lease.token)
    assert claims.nodes.get_findings("T1") == before


def test_an_approve_with_minor_findings_lands_then_fixes_them_and_completes(
    tmp_path: Path,
) -> None:
    claims = make_estate(tmp_path, config=gated("api"))
    claims.landing = FakeLanding()
    add(claims, "T1", status=Status.IMPLEMENTED)
    claims.start("T1", "reviewer", "s1")
    claims.add_finding("T1", Severity.MINOR, "a.py:2", "the name hides the unit")
    claims.review("T1", approve=True)
    assert claims.start("T1", "lander", "s1").action == Action.MERGE

    assert claims.landed("T1", "landed") == Status.REVIEWED
    fix = claims.start("T1", "fixer", "s1")
    assert fix.action == Action.FIX and fix.worktree is not None
    fixed = commit(Path(fix.worktree), "a.py", "seconds = 1\n", "name the unit")
    claims.answer_finding("T1", 1, FindingState.CLOSED, fixed)
    assert claims.complete("T1") == Status.FIXED
    assert claims.start("T1", "lander", "s1").action == Action.MERGE
    assert claims.landed("T1", "landed") == Status.COMPLETED


def test_a_landed_plan_approved_with_minor_findings_fixes_them_before_completing(
    tmp_path: Path,
) -> None:
    claims = make_estate(tmp_path, config=gated("api"))
    add(claims, "P", NodeKind.PLAN, review=True, fix=True, status=Status.LANDED)
    claims.start("P", "reviewer", "s1")
    claims.add_finding("P", Severity.MINOR, "a.py:2", "the name hides the unit")

    assert claims.review("P", approve=True) == Status.REVIEWED
    assert claims.start("P", "fixer", "s1").action == Action.FIX


def test_a_failed_landing_records_a_blocking_landing_finding(tmp_path: Path) -> None:
    claims = make_estate(tmp_path, config=gated("api"))
    add(claims, "T1", status=Status.REVIEWED, outcome=Outcome.APPROVE)
    claims.landing = FakeLanding()
    claims.start("T1", "lander", "s1")

    assert claims.landing_failed("T1", "the gate fails on a.py") == Status.REVIEWED
    [finding] = claims.nodes.get_findings("T1")
    assert (finding.source, finding.severity, finding.at, finding.breaks, finding.state) == (
        FindingSource.LANDING,
        Severity.BLOCKING,
        "landing",
        "the gate fails on a.py",
        FindingState.OPEN,
    )


@pytest.mark.parametrize(
    ("merge", "branch", "base"),
    [(Merge.SPEC, "origin/main", "main"), (Merge.PARENT, "tm/S", "tm/S")],
)
def test_a_review_of_a_landed_plan_reads_the_target_it_landed_on_in_every_repository(
    tmp_path: Path, merge: Merge, branch: str, base: str
) -> None:
    claims = make_estate(tmp_path, repos=("api", "web"), config=gated("api", "web"))
    add(claims, "S", NodeKind.SPEC)
    add(claims, "P", NodeKind.PLAN, parent="S", merge=merge, review=True, status=Status.LANDED)
    add(claims, "A", parent="P", repo="api", review=False, fix=False, status=Status.COMPLETED)
    add(claims, "W", parent="P", repo="web", review=False, fix=False, status=Status.COMPLETED)
    branch_at(claims.root / "api", "tm/S")

    result = claims.start("P", "reviewer", "s1")

    assert (result.action, result.branch, result.base) == (Action.REVIEW, branch, base)
    assert (result.repos, result.worktree) == (["api", "web"], None)
    lease = claims.runtime.get_lease("P")
    assert lease is not None and lease.branch_name == branch


def test_a_landed_plan_s_fix_is_cut_from_its_target_in_every_repository_its_code_landed_in(
    tmp_path: Path,
) -> None:
    claims = make_estate(tmp_path, repos=("api", "web"), config=gated("api", "web"))
    api = claims.root / "api"
    add(
        claims,
        "P",
        NodeKind.PLAN,
        review=True,
        fix=True,
        status=Status.REVIEWED,
        outcome=Outcome.REJECT,
        review_cycles=1,
    )
    add(claims, "A", parent="P", repo="api", merge=Merge.PARENT, status=Status.COMPLETED)
    add(claims, "W", parent="P", repo="web", status=Status.COMPLETED)
    landed = on_branch(api, "tm/P", "p.py", "p = 1\n")
    git(api, "push", "-q", "origin", "tm/P:main")
    later = push_main(api, "q.py", "q = 1\n")

    result = claims.start("P", "fixer", "s1")

    assert result.action == Action.FIX
    assert sorted(result.worktrees) == ["api", "web"]
    assert git(Path(result.worktrees["api"]), "rev-parse", "HEAD") == later
    assert git(Path(result.worktrees["web"]), "rev-parse", "--abbrev-ref", "HEAD") == "tm/P"
    assert git(api, "rev-parse", "tm/P@1") == landed


def test_a_fix_whose_branch_carries_nothing_of_its_own_is_cut_from_its_target(
    tmp_path: Path,
) -> None:
    claims = make_estate(tmp_path, config=gated("api"))
    api = claims.root / "api"
    add(claims, "T1", status=Status.REVIEWED, outcome=Outcome.REJECT, review_cycles=1)
    branch_at(api, "tm/T1")
    later = push_main(api, "q.py", "q = 1\n")
    scratch = tmp_path / "sync"
    git(api, "worktree", "add", "-q", str(scratch), "tm/T1")
    git(scratch, "merge", "-q", "--no-ff", "--no-edit", "origin/main")
    git(api, "worktree", "remove", "--force", str(scratch))
    assert git(api, "rev-parse", "tm/T1") != later

    result = claims.start("T1", "fixer", "s1")

    assert result.worktree is not None
    assert git(Path(result.worktree), "rev-parse", "HEAD") == later


MIGRATION = "api/migrations/versions/0042_add_tenant.py"


@pytest.mark.parametrize(
    ("frontmatter", "verified", "action", "shown"),
    [
        ({}, None, Action.MERGE, "WAITING_MERGE"),
        ({"sensitive": "tenant"}, None, Action.REVIEW, "WAITING_REVIEW"),
        ({"declared_files": [MIGRATION]}, None, Action.REVIEW, "WAITING_REVIEW"),
        ({}, MIGRATION, Action.REVIEW, "WAITING_REVIEW"),
    ],
    ids=["plain", "sensitive-key", "declared-migration", "verified-migration"],
)
def test_a_fix_lands_as_it_is_unless_the_node_is_sensitive_and_owes_its_fix_one_review(
    tmp_path: Path,
    frontmatter: dict[str, object],
    verified: str | None,
    action: Action,
    shown: str,
) -> None:
    claims = make_estate(tmp_path, config=gated("api"))
    claims.landing = FakeLanding()
    add(
        claims,
        "T1",
        status=Status.FIXED,
        outcome=Outcome.REJECT,
        fix_for=Outcome.REJECT,
        review_cycles=1,
    )
    claims.nodes.save_node(stored(claims, "T1").model_copy(update={"frontmatter": frontmatter}))
    if verified is not None:
        claims.nodes.add_verification(
            NodeVerification(
                node_id="T1", verification_type=VerificationType.FILE_EXISTS, target_path=verified
            )
        )
    on_branch(claims.root / "api", "tm/T1", "a.py", "a = 1\n")

    assert DisplayView(claims.snapshots).display(stored(claims, "T1")) == shown
    assert claims.start("T1", "agent", "s1").action == action


def test_a_landed_plan_rejected_with_fix_off_fails_and_opens_a_decision(tmp_path: Path) -> None:
    claims = make_estate(tmp_path, config=gated("api"))
    add(claims, "P", NodeKind.PLAN, review=True, fix=False, status=Status.LANDED)

    assert claims.start("P", "reviewer", "s1").action == Action.REVIEW
    claims.add_finding("P", Severity.BLOCKING, "gate", "the landed target breaks the gate")

    assert claims.review("P", approve=False) == Status.FAILED
    assert len(decisions_blocking(claims, "P")) == 1


def test_transient_releases_count_step_failures_until_the_cap_fails_the_node(
    tmp_path: Path,
) -> None:
    claims = make_estate(tmp_path, config=gated("api"))
    add(claims, "T1")
    for failures in (1, 2):
        claims.start("T1", "agent-1", "s1")
        assert claims.release("T1") == Status.READY
        assert stored(claims, "T1").step_failures == failures

    claims.start("T1", "agent-1", "s1")
    assert claims.release("T1") == Status.FAILED
    assert len(decisions_blocking(claims, "T1")) == 1


def test_a_blocked_release_writes_what_the_node_waits_on_without_counting_a_failure(
    tmp_path: Path,
) -> None:
    claims = make_estate(tmp_path, config=gated("api"))
    add(claims, "T1")
    add(claims, "D")
    claims.start("T1", "agent-1", "s1")

    assert claims.release("T1", Blocker(depends=["D"])) == Status.READY
    assert "D" in claims.nodes.get_dependencies("T1")
    assert stored(claims, "T1").step_failures == 0
    assert claims.start("T1", "agent-1", "s1").reason == "waits on D"


def test_a_blocked_release_naming_nothing_is_refused(tmp_path: Path) -> None:
    claims = make_estate(tmp_path, config=gated("api"))
    add(claims, "T1")
    claims.start("T1", "agent-1", "s1")
    with pytest.raises(OperationError) as refused:
        claims.release("T1", Blocker())
    assert refused.value.status_code == 400
    assert stored(claims, "T1").status == Status.IMPLEMENTING


def test_a_blocked_release_with_a_prose_condition_is_refused(tmp_path: Path) -> None:
    claims = make_estate(tmp_path, config=gated("api"))
    add(claims, "T1")
    claims.start("T1", "agent-1", "s1")
    prose = Condition(
        node_id="T1",
        idx=0,
        needs="design approved",
        command="the designer approves the frame",
        stage=ConditionStage.CLAIM,
    )
    with pytest.raises(OperationError, match="decision"):
        claims.release("T1", Blocker(condition=prose))
    assert stored(claims, "T1").status == Status.IMPLEMENTING


def test_a_blocked_release_that_closes_a_cycle_is_refused_with_the_cycle(tmp_path: Path) -> None:
    claims = make_estate(tmp_path, config=gated("api"))
    add(claims, "T1")
    add(claims, "D", depends=("T1",))
    claims.start("T1", "agent-1", "s1")
    with pytest.raises(OperationError, match="←") as refused:
        claims.release("T1", Blocker(depends=["D"]))
    assert refused.value.status_code == 409
    assert "D" not in claims.nodes.get_dependencies("T1")
    assert claims.runtime.get_lease("T1") is not None


def test_sweep_returns_an_expired_claim_to_where_it_was_claimed_from(tmp_path: Path) -> None:
    claims = make_estate(tmp_path, config=gated("api"))
    add(claims, "T1", status=Status.IMPLEMENTED)
    claims.start("T1", "reviewer", "s1", ttl=1)
    time.sleep(1.5)

    assert claims.sweep() == ["T1"]
    node = stored(claims, "T1")
    assert (node.status, node.step_failures, node.claimed_from) == (Status.IMPLEMENTED, 1, None)


def test_sweep_restores_a_mid_step_node_with_no_lease_row(tmp_path: Path) -> None:
    claims = make_estate(tmp_path, config=gated("api"))
    add(
        claims,
        "T1",
        status=Status.REVIEWING,
        claimed_from=Status.IMPLEMENTED,
        review_cycles=1,
    )

    assert claims.sweep() == ["T1"]
    assert stored(claims, "T1").status == Status.IMPLEMENTED


def test_a_merge_claim_starts_a_landing_job_and_returns_at_once(tmp_path: Path) -> None:
    claims = make_estate(tmp_path, config=gated("api"))
    add(claims, "T1", status=Status.REVIEWED, outcome=Outcome.APPROVE, review_cycles=1)
    landing = FakeLanding()
    claims.landing = landing

    result = claims.start("T1", "merger", "s1")

    assert (result.action, result.job, result.model) == (Action.MERGE, "job-1", "claude-sonnet-5-5")
    assert landing.started == ["T1"]
    assert stored(claims, "T1").status == Status.MERGING
    lease = claims.runtime.get_lease("T1")
    assert lease is not None and lease.action == Action.MERGE


def test_a_landing_waiting_for_an_agent_is_handed_to_the_next_claimant(tmp_path: Path) -> None:
    claims = make_estate(tmp_path, config=gated("api"))
    add(claims, "T1", status=Status.REVIEWED, outcome=Outcome.APPROVE, review_cycles=1)
    claims.landing = FakeLanding()
    claims.start("T1", "merger", "s1")
    job = claims.jobs.create(
        Job(
            kind=JobKind.LAND,
            node_id="T1",
            repo="api",
            target="main",
            state=JobState.NEEDS_AGENT,
            step="gate",
            worktree="/tmp/landing-worktree",
            result={"reason": "conflict"},
        )
    )
    claims.runtime.park("T1")

    handed = claims.start("T1", "agent-2", "s2")

    assert (handed.action, handed.job, handed.worktree, handed.worktrees) == (
        Action.MERGE,
        job.id,
        "/tmp/landing-worktree",
        {"api": "/tmp/landing-worktree"},
    )
    assert (handed.base, handed.bases, handed.remotes) == (
        "main",
        {"api": "main"},
        {"api": "origin"},
    )
    lease = claims.runtime.get_lease("T1")
    assert lease is not None
    assert (lease.agent_id, lease.ttl_seconds, lease.model) == (
        "agent-2",
        claims.ttl_for(Action.MERGE),
        "claude-sonnet-5-5",
    )
    assert claims.start("T1", "agent-3", "s3").action == Action.BLOCKED


def test_a_container_whose_children_completed_with_nothing_on_its_branch_completes(
    tmp_path: Path,
) -> None:
    claims = make_estate(tmp_path, config=gated("api"))
    add(claims, "P", NodeKind.PLAN)
    add(claims, "T1", parent="P", status=Status.REVIEWED, outcome=Outcome.APPROVE)
    branch_at(claims.root / "api", "tm/T1")

    assert claims.reset("T1", Status.COMPLETED, "landed by hand") == Status.COMPLETED
    assert stored(claims, "P").status == Status.COMPLETED
    assert "landed by hand" in section(claims, "T1", "reset")


def test_a_container_with_code_on_its_branch_stops_at_implemented(tmp_path: Path) -> None:
    claims = make_estate(tmp_path, config=gated("api"))
    api = claims.root / "api"
    add(claims, "P", NodeKind.PLAN)
    add(
        claims,
        "T1",
        parent="P",
        merge=Merge.PARENT,
        status=Status.REVIEWED,
        outcome=Outcome.APPROVE,
    )
    branch_at(api, "tm/T1")
    on_branch(api, "tm/P", "p.py", "p = 1\n")

    claims.reset("T1", Status.COMPLETED, "landed by hand")

    assert stored(claims, "P").status == Status.IMPLEMENTED


@pytest.mark.parametrize("to", [Status.LANDED, Status.COMPLETED])
def test_a_reset_to_landed_code_is_refused_while_the_branch_is_not_on_its_target(
    tmp_path: Path, to: Status
) -> None:
    claims = make_estate(tmp_path, config=gated("api"))
    add(claims, "T1", status=Status.REVIEWED, outcome=Outcome.APPROVE)
    on_branch(claims.root / "api", "tm/T1", "a.py", "a = 1\n")

    with pytest.raises(OperationError, match="is not on main"):
        claims.reset("T1", to, "landed by hand")
    assert stored(claims, "T1").status == Status.REVIEWED


def test_deferring_a_node_others_depend_on_opens_one_decision_for_the_dependents(
    tmp_path: Path,
) -> None:
    claims = make_estate(tmp_path, config=gated("api"))
    add(claims, "D")
    add(claims, "T1", depends=("D",))
    add(claims, "T2", depends=("D",))

    assert claims.defer("D", "waiting on the vendor") == Status.DEFERRED

    assert "waiting on the vendor" in section(claims, "D", "deferral")
    assert decisions_blocking(claims, "T1") == decisions_blocking(claims, "T2")
    assert len(decisions_blocking(claims, "T1")) == 1


def test_defer_is_refused_mid_step(tmp_path: Path) -> None:
    claims = make_estate(tmp_path, config=gated("api"))
    add(claims, "T1")
    claims.start("T1", "agent-1", "s1")
    with pytest.raises(OperationError) as refused:
        claims.defer("T1", "later")
    assert refused.value.status_code == 409


def test_reopen_returns_a_deferred_task_to_ready_with_counters_cleared_and_the_note_kept(
    tmp_path: Path,
) -> None:
    claims = make_estate(tmp_path, config=gated("api"))
    add(claims, "T1", status=Status.DEFERRED, step_failures=2, review_cycles=1)

    assert claims.reopen("T1", "the vendor shipped") == Status.READY

    node = stored(claims, "T1")
    assert (node.step_failures, node.review_cycles, node.outcome) == (0, 0, None)
    assert "the vendor shipped" in section(claims, "T1", "reopen")


def test_reopen_is_refused_while_an_open_decision_blocks_the_node(tmp_path: Path) -> None:
    claims = make_estate(tmp_path, config=gated("api"))
    add(claims, "T1", status=Status.FAILED)
    claims.ops.add_decision("Abandon T1?", blocks=["T1"])

    with pytest.raises(OperationError, match="open decision"):
        claims.reopen("T1", "try again")
    assert stored(claims, "T1").status == Status.FAILED


def test_reopen_with_a_new_branch_keeps_the_old_one_under_a_numbered_name(tmp_path: Path) -> None:
    claims = make_estate(tmp_path, config=gated("api"))
    api = claims.root / "api"
    add(claims, "T1", status=Status.ABANDONED)
    old = on_branch(api, "tm/T1", "a.py", "a = 1\n")

    claims.reopen("T1", "start over", new_branch=True)

    assert git(api, "rev-parse", "tm/T1@1") == old
    assert git(api, "branch", "--list", "tm/T1") == ""


def test_a_node_the_lifecycle_refuses_to_expire_does_not_stop_the_sweep_for_its_siblings(
    tmp_path: Path,
) -> None:
    from datetime import UTC, datetime, timedelta

    from taskmanager.core.models import Lease

    claims = make_estate(tmp_path, config=gated("api"))
    add(claims, "BAD", status=Status.FAILED)
    add(claims, "OK", status=Status.IMPLEMENTING, claimed_from=Status.READY)
    stale = datetime.now(tz=UTC) - timedelta(hours=1)
    for node_id in ("BAD", "OK"):
        claims.runtime.acquire_lease(
            Lease(
                task_id=node_id,
                agent_id="gone",
                session_id="s",
                branch_name=f"tm/{node_id}",
                ttl_seconds=60,
                last_heartbeat=stale,
            ),
            [],
        )
    claims.jobs.create(Job(kind=JobKind.LAND, node_id="BAD", repo="api", target="main"))

    assert sorted(claims.sweep()) == ["BAD", "OK"]

    assert stored(claims, "OK").status == Status.READY
    assert stored(claims, "BAD").status == Status.FAILED


def test_a_child_reopens_under_its_rolled_up_exit_only_once_the_container_is_reopened(
    tmp_path: Path,
) -> None:
    claims = make_estate(tmp_path, config=gated("api"))
    add(claims, "P", NodeKind.PLAN)
    add(claims, "P-a", parent="P")
    claims.defer("P-a", "later")
    assert stored(claims, "P").status == Status.DEFERRED

    with pytest.raises(OperationError, match="reopen P first"):
        claims.reopen("P-a", "back")
    assert stored(claims, "P-a").status == Status.DEFERRED

    claims.reopen("P", "back")
    claims.reopen("P-a", "back")
    assert (stored(claims, "P").status, stored(claims, "P-a").status) == (
        Status.READY,
        Status.READY,
    )


def test_a_container_claimed_just_before_a_reopen_commits_refuses_the_reopen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    claims = make_estate(tmp_path, config=gated("api"))
    add(claims, "P", NodeKind.PLAN, review=True, fix=True, status=Status.IMPLEMENTED)
    add(claims, "U", parent="P", status=Status.COMPLETED)
    add(claims, "T", parent="P", status=Status.DEFERRED)
    real = claims.nodes.transaction

    def claimed_first() -> Any:
        monkeypatch.setattr(claims.nodes, "transaction", real)
        claims.nodes.save_node(
            stored(claims, "P").model_copy(
                update={"status": Status.REVIEWING, "claimed_from": Status.IMPLEMENTED}
            )
        )
        return real()

    monkeypatch.setattr(claims.nodes, "transaction", claimed_first)
    with pytest.raises(OperationError, match="P is in a step"):
        claims.reopen("T", "back")
    assert stored(claims, "T").status == Status.DEFERRED


def test_a_container_lands_locks_and_verifies_only_the_descendants_it_still_counts(
    tmp_path: Path,
) -> None:
    from taskmanager.core.enums import VerificationType
    from taskmanager.core.models import NodeVerification

    claims = make_estate(tmp_path, repos=("api", "web"), config=gated("api", "web"))
    add(
        claims,
        "P",
        NodeKind.PLAN,
        review=True,
        fix=True,
        status=Status.REVIEWED,
        outcome=Outcome.REJECT,
        review_cycles=1,
    )
    add(claims, "T1", parent="P", merge=Merge.PARENT, status=Status.COMPLETED, files=["api/a.py"])
    add(
        claims,
        "T2",
        parent="P",
        merge=Merge.PARENT,
        status=Status.ABANDONED,
        repo="web",
        files=["web/b.py"],
    )
    add(claims, "Q", NodeKind.PLAN, parent="P", merge=Merge.PARENT, status=Status.DEFERRED)
    add(claims, "T3", parent="Q", merge=Merge.PARENT, repo="web", files=["web/c.py"])
    for node_id, path in (("T1", "api/README.md"), ("T2", "web/missing.py"), ("T3", "web/x.py")):
        claims.nodes.add_verification(
            NodeVerification(
                node_id=node_id, verification_type=VerificationType.FILE_EXISTS, target_path=path
            )
        )
    claims.landing = FakeLanding()

    assert claims.repos_of("P") == ["api"]
    passed, report, _ = claims.verify("P", "origin/main")
    assert passed, report
    assert claims.start("P", "fixer", "s1").action == Action.FIX
    assert {lock.file_path for lock in claims.runtime.list_locks()} == {
        "api:api/README.md",
        "api:api/a.py",
    }


def test_a_step_closes_only_under_the_token_its_claim_returned(tmp_path: Path) -> None:
    claims = make_estate(tmp_path, config=gated("api"))
    add(claims, "T1")
    add(claims, "T2", status=Status.IMPLEMENTED)
    first = claims.start("T1", "worker", "s1")
    review = claims.start("T2", "worker", "s1")
    assert first.token and review.token and first.token != review.token
    lease = claims.runtime.get_lease("T1")
    assert lease is not None and lease.token == first.token

    for close in (
        lambda: claims.complete("T1", agent="worker", token=review.token),
        lambda: claims.release("T1", agent="worker", token=review.token),
        lambda: claims.review("T2", approve=True, clean=True, agent="worker", token=first.token),
    ):
        with pytest.raises(OperationError, match="another claim") as refused:
            close()
        assert refused.value.status_code == 409

    assert claims.complete("T1", agent="worker", token=first.token) == Status.IMPLEMENTED
    assert claims.review("T2", approve=True, clean=True, token=review.token) == Status.REVIEWED


def test_a_handed_over_job_gets_a_new_token_and_the_parked_one_closes_nothing(
    tmp_path: Path,
) -> None:
    claims = make_estate(tmp_path, config=gated("api"))
    add(claims, "T1", status=Status.REVIEWED, outcome=Outcome.APPROVE, review_cycles=1)
    claims.landing = FakeLanding()
    merged = claims.start("T1", "merger", "s1")
    claims.jobs.create(
        Job(
            kind=JobKind.LAND,
            node_id="T1",
            repo="api",
            target="main",
            state=JobState.NEEDS_AGENT,
            step="gate",
            result={"reason": "conflict"},
        )
    )
    claims.runtime.park("T1")

    handed = claims.start("T1", "merger", "s1")

    assert handed.token and handed.token != merged.token
    with pytest.raises(OperationError, match="another claim"):
        claims.release("T1", token=merged.token)
    assert claims.release("T1", token=handed.token) == Status.REVIEWED


def test_a_node_reopened_on_a_new_branch_is_cut_a_fresh_worktree_beside_the_retired_one(
    tmp_path: Path,
) -> None:
    claims = make_estate(tmp_path, config=gated("api"))
    add(claims, "T1")
    first = claims.start("T1", "agent", "s1")
    assert first.worktree is not None
    (Path(first.worktree) / "draft.py").write_text("unfinished\n")
    claims.release("T1")
    claims.defer("T1", "later")
    claims.reopen("T1", "start clean", new_branch=True)

    again = claims.start("T1", "agent", "s1")

    assert again.action == Action.IMPLEMENT
    assert again.worktree is not None
    worktree = Path(again.worktree)
    assert git(worktree, "rev-parse", "--abbrev-ref", "HEAD") == "tm/T1"
    assert not (worktree / "draft.py").exists()
    retired = Path(f"{first.worktree}@1")
    assert git(retired, "rev-parse", "--abbrev-ref", "HEAD") == "tm/T1@1"
    assert (retired / "draft.py").exists()


def test_a_worktree_path_taken_by_another_branch_undoes_the_claim(tmp_path: Path) -> None:
    claims = make_estate(tmp_path, config=gated("api"))
    add(claims, "T1")
    taken = claims.root / claims.config.worktree_dir / "api-T1"
    git(claims.root / "api", "worktree", "add", "-q", "-b", "other", str(taken), "origin/main")

    with pytest.raises(OperationError, match="claim of T1 undone"):
        claims.start("T1", "agent", "s1")

    node = stored(claims, "T1")
    assert (node.status, node.claimed_from) == (Status.READY, None)
    assert claims.runtime.get_lease("T1") is None


def test_an_investigate_answer_cannot_reopen_a_child_under_a_set_aside_container(
    tmp_path: Path,
) -> None:
    from taskmanager.engine.decisions import open_failed_decision

    claims = make_estate(tmp_path, config=gated("api"))
    add(claims, "P", NodeKind.PLAN)
    add(claims, "P-a", parent="P", status=Status.FAILED)
    add(claims, "P-b", parent="P")
    decision = open_failed_decision(claims.ops, "P-a", "its step failed", "")
    claims.defer("P", "later")

    with pytest.raises(OperationError, match="reopen P first"):
        claims.ops.answer_decision(decision, option="investigate")

    assert stored(claims, "P-a").status == Status.FAILED
    assert stored(claims, decision).status == "OPEN"


@pytest.mark.parametrize(
    ("status", "claimed_from", "refusal"),
    [
        (Status.FAILED, None, "reopen P first"),
        (Status.MERGING, Status.IMPLEMENTED, "P is in a step"),
    ],
    ids=["failed", "in-a-step"],
)
def test_an_investigate_answer_cannot_reopen_a_child_under_a_failed_or_stepping_container(
    tmp_path: Path, status: Status, claimed_from: Status | None, refusal: str
) -> None:
    from taskmanager.engine.decisions import open_failed_decision

    claims = make_estate(tmp_path, config=gated("api"))
    add(claims, "P", NodeKind.PLAN, status=status, claimed_from=claimed_from)
    add(claims, "P-a", parent="P", status=Status.FAILED)
    decision = open_failed_decision(claims.ops, "P-a", "its step failed", "")

    with pytest.raises(OperationError, match=refusal):
        claims.ops.answer_decision(decision, option="investigate")

    assert stored(claims, "P-a").status == Status.FAILED
    assert stored(claims, decision).status == "OPEN"


def test_an_investigate_answer_ends_a_parked_landing_s_red_target_wait(tmp_path: Path) -> None:
    from taskmanager.engine.decisions import open_failed_decision

    claims = make_estate(tmp_path, config=gated("api"))
    add(claims, "T1", status=Status.FAILED)
    claims.nodes.add_condition(
        Condition(
            node_id="T1",
            idx=0,
            needs="red-target: api main at abc fails the 1 test(s) this landing fails",
            command="true",
            stage=ConditionStage.LANDING,
        )
    )
    decision = open_failed_decision(claims.ops, "T1", "its step failed", "")

    claims.ops.answer_decision(decision, option="investigate")

    assert stored(claims, "T1").status == Status.READY
    assert claims.nodes.get_conditions("T1") == []


def test_reopening_a_container_on_a_new_branch_retires_it_where_only_a_set_aside_child_worked(
    tmp_path: Path,
) -> None:
    claims = make_estate(tmp_path, repos=("api", "web"), config=gated("api", "web"))
    add(claims, "P", NodeKind.PLAN, status=Status.DEFERRED)
    add(claims, "P-a", parent="P", repo="api", status=Status.DEFERRED)
    add(claims, "P-b", parent="P", repo="web")
    for repo in ("api", "web"):
        branch_at(claims.root / repo, "tm/P")

    claims.reopen("P", "start over", new_branch=True)

    for repo in ("api", "web"):
        assert git(claims.root / repo, "branch", "--list", "tm/P*").split() == ["tm/P@1"]


def test_main_and_a_container_branch_red_at_one_sha_each_get_their_own_decision(
    tmp_path: Path,
) -> None:
    claims = make_estate(tmp_path, config=gated("api"))
    sha = "a" * 40
    for node_id, target in (("T1", "main"), ("T2", "tm/P")):
        add(claims, node_id)
        claims.nodes.add_condition(
            Condition(
                node_id=node_id,
                idx=0,
                needs=f"red-target: api {target} at {sha[:12]} fails the 1 test(s)",
                command="true",
                stage=ConditionStage.LANDING,
            )
        )
        mark = {
            "repo": "api",
            "target": target,
            "sha": sha,
            "since": "2000-01-01T00:00:00+00:00",
            "failing": ["suite.t"],
        }
        claims.jobs.create(
            Job(
                kind=JobKind.LAND,
                node_id=node_id,
                repo="api",
                target=target,
                state=JobState.EXPIRED,
                result={"red_target": mark},
            )
        )

    opened = claims._escalate_red_targets()

    assert len(opened) == 2
    for node_id, target in (("T1", "main"), ("T2", "tm/P")):
        [decision] = [d for d in opened if d in claims.nodes.get_dependencies(node_id)]
        assert stored(claims, decision).title.startswith(f"{target} of api is red")


def test_a_red_target_decision_id_holds_no_slash_from_its_repository_or_target(
    tmp_path: Path,
) -> None:
    claims = make_estate(tmp_path, config=gated("api"))
    sha = "a" * 40
    add(claims, "T1")
    claims.nodes.add_condition(
        Condition(
            node_id="T1",
            idx=0,
            needs=f"red-target: worktrees/api tm/P at {sha[:12]} fails the 1 test(s)",
            command="true",
            stage=ConditionStage.LANDING,
        )
    )
    mark = {
        "repo": "worktrees/api",
        "target": "tm/P",
        "sha": sha,
        "since": "2000-01-01T00:00:00+00:00",
        "failing": ["suite.t"],
    }
    claims.jobs.create(
        Job(
            kind=JobKind.LAND,
            node_id="T1",
            repo="api",
            target="tm/P",
            state=JobState.EXPIRED,
            result={"red_target": mark},
        )
    )

    opened = claims._escalate_red_targets()

    assert opened == [f"decision-red-target-worktrees-api-tm-P-{sha[:12]}"]


def test_an_open_red_target_decision_stored_with_its_repository_s_slash_is_reused(
    tmp_path: Path,
) -> None:
    claims = make_estate(tmp_path, config=gated("api"))
    sha = "a" * 40
    legacy = f"decision-red-target-worktrees/api-main-{sha[:12]}"
    claims.nodes.save_node(
        Node(
            id=legacy,
            kind=NodeKind.DECISION,
            title="main of worktrees/api is red",
            frontmatter={"decision": {"options": [], "allow_custom": True, "answer": None}},
        )
    )
    add(claims, "T1")
    claims.nodes.add_condition(
        Condition(
            node_id="T1",
            idx=0,
            needs=f"red-target: worktrees/api main at {sha[:12]} fails the 1 test(s)",
            command="true",
            stage=ConditionStage.LANDING,
        )
    )
    mark = {
        "repo": "worktrees/api",
        "target": "main",
        "sha": sha,
        "since": "2000-01-01T00:00:00+00:00",
        "failing": ["suite.t"],
    }
    claims.jobs.create(
        Job(
            kind=JobKind.LAND,
            node_id="T1",
            repo="api",
            target="main",
            state=JobState.EXPIRED,
            result={"red_target": mark},
        )
    )

    assert claims._escalate_red_targets() == []
    assert claims.nodes.get_dependencies("T1") == [legacy]


def test_escalating_red_targets_selects_every_node_only_once(tmp_path: Path) -> None:
    claims = make_estate(tmp_path, config=gated("api"))
    add(claims, "T1")

    node_selects: list[str] = []

    def trace(sql: str) -> None:
        if "FROM nodes" in sql:
            node_selects.append(sql)

    with claims.nodes.db.get_state_connection() as conn:
        conn.set_trace_callback(trace)
    try:
        claims._escalate_red_targets()
    finally:
        with claims.nodes.db.get_state_connection() as conn:
            conn.set_trace_callback(None)

    # `read_graph`'s own two selects (columns, then revisions), never `list_nodes`'s
    # `WHERE 1=1` on top of them.
    assert len(node_selects) == 2
    assert all("WHERE 1=1" not in sql for sql in node_selects)


def test_counting_a_stopped_job_keeps_an_edit_that_committed_after_the_count_began(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    claims = make_estate(tmp_path, config=gated("api"))
    add(claims, "T1")
    real = claims.nodes.transaction

    def renamed_first() -> Any:
        monkeypatch.setattr(claims.nodes, "transaction", real)
        claims.ops.update_node("T1", title="Renamed")
        return real()

    monkeypatch.setattr(claims.nodes, "transaction", renamed_first)
    claims.count_unresolved("T1")

    node = stored(claims, "T1")
    assert (node.title, node.step_failures) == ("Renamed", 1)


def test_two_counts_racing_below_the_cap_fail_the_node_that_reaches_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    claims = make_estate(tmp_path, config=gated("api"))
    add(claims, "T1")
    claims.start("T1", "agent-1", "s1")
    below = claims.caps.step_failures - 2
    claims.nodes.save_node(stored(claims, "T1").model_copy(update={"step_failures": below}))
    real = claims.nodes.transaction
    # Both counts reach the write lock before either takes it: a cap read outside the lock is
    # then stale for whichever count commits second.
    barrier = threading.Barrier(2, timeout=10)

    def both_at_the_lock() -> Any:
        if not claims.nodes.db.in_transaction:
            barrier.wait()
        return real()

    monkeypatch.setattr(claims.nodes, "transaction", both_at_the_lock)
    threads = [threading.Thread(target=claims.count_unresolved, args=("T1",)) for _ in "ab"]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    node = stored(claims, "T1")
    assert (node.status, node.step_failures) == (Status.FAILED, claims.caps.step_failures)


def test_a_complete_is_refused_while_a_deletion_s_latest_check_is_green(tmp_path: Path) -> None:
    claims = make_estate(tmp_path, config=gated("api"))
    add(claims, "T1")
    claims.start("T1", "implementer", "s1")
    claims.add_check("T1", "a.py:guard_null", "test_null_id")
    claims.add_check("T1", "a.py:guard_empty", None)
    claims.add_check("T1", "a.py:escape", None)

    with pytest.raises(OperationError, match="a.py:guard_empty, a.py:escape") as refused:
        claims.complete("T1")
    assert refused.value.status_code == 409

    claims.add_check("T1", " a.py:guard_empty ", "test_empty_id")
    with pytest.raises(OperationError, match="passing: a.py:escape. Add"):
        claims.complete("T1")
    claims.add_check("T1", "a.py:escape", "test_quote")
    assert claims.complete("T1") == Status.IMPLEMENTED
    assert [(c.number, c.deleted, c.result, c.test) for c in claims.nodes.get_checks("T1")] == [
        (1, "a.py:guard_null", CheckResult.RED, "test_null_id"),
        (2, "a.py:guard_empty", CheckResult.GREEN, None),
        (3, "a.py:escape", CheckResult.GREEN, None),
        (4, "a.py:guard_empty", CheckResult.RED, "test_empty_id"),
        (5, "a.py:escape", CheckResult.RED, "test_quote"),
    ]


def test_a_green_check_after_a_red_one_of_the_same_deletion_refuses_the_complete(
    tmp_path: Path,
) -> None:
    claims = make_estate(tmp_path, config=gated("api"))
    add(claims, "T1")
    claims.start("T1", "implementer", "s1")
    claims.add_check("T1", "a.py:guard", "test_guard")
    claims.add_check("T1", "a.py:guard", None)

    with pytest.raises(OperationError, match="passing: a.py:guard"):
        claims.complete("T1")


def test_a_fix_complete_is_refused_while_its_deletion_stands_green(tmp_path: Path) -> None:
    claims = make_estate(tmp_path, config=gated("api"))
    _rejected_and_fixing(claims)
    claims.answer_finding("T1", 1, FindingState.DISPUTED, "the id is never null")
    claims.answer_finding("T1", 2, FindingState.DISPUTED, "the unit is in the type")
    claims.add_check("T1", "a.py:scope", None)

    with pytest.raises(OperationError, match="passing: a.py:scope"):
        claims.complete("T1")


def test_a_check_and_a_note_are_recorded_only_under_the_implement_or_fix_lease(
    tmp_path: Path,
) -> None:
    claims = make_estate(tmp_path, config=gated("api"))
    add(claims, "T1")
    add(claims, "T2", status=Status.IMPLEMENTED)
    claims.start("T1", "implementer", "s1")
    claims.start("T2", "reviewer", "s1")

    with pytest.raises(OperationError, match="IMPLEMENTING"):
        claims.add_check("T2", "a.py:guard", "test_guard")
    with pytest.raises(OperationError, match="IMPLEMENTING"):
        claims.add_note("T2", "x")
    with pytest.raises(OperationError, match="holds no live lease"):
        claims.add_check("T1", "a.py:guard", "test_guard", agent="intruder")
    with pytest.raises(OperationError, match="holds no live lease"):
        claims.add_note("T1", "x", agent="intruder")
    assert claims.nodes.get_checks("T1") == [] and claims.nodes.get_notes("T1") == []


@pytest.mark.parametrize(
    ("deleted", "test"), [("", "t"), (" ", "t"), ("a.py:g", ""), ("a.py:g", " ")]
)
def test_a_check_names_its_deletion_and_a_red_one_its_test(
    tmp_path: Path, deleted: str, test: str
) -> None:
    claims = make_estate(tmp_path, config=gated("api"))
    add(claims, "T1")
    claims.start("T1", "implementer", "s1")

    with pytest.raises(OperationError, match="--deleted"):
        claims.add_check("T1", deleted, test)
    assert claims.nodes.get_checks("T1") == []


@pytest.mark.parametrize("text", ["", " ", "two\nlines"])
def test_a_note_is_one_line(tmp_path: Path, text: str) -> None:
    claims = make_estate(tmp_path, config=gated("api"))
    add(claims, "T1")
    claims.start("T1", "implementer", "s1")

    with pytest.raises(OperationError, match="one line"):
        claims.add_note("T1", text)
    assert claims.add_note("T1", "  the brief names a.py; the tree has b.py ").text == (
        "the brief names a.py; the tree has b.py"
    )
    assert claims.add_note("T1", "verify row 2 red").number == 2


@pytest.mark.parametrize("write", ["check", "note"])
def test_a_check_or_note_is_refused_when_the_lease_lapses_before_the_write_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, write: str
) -> None:
    claims = make_estate(tmp_path, config=gated("api"))
    add(claims, "T1")
    claims.start("T1", "implementer", "s1")
    lease = claims.runtime.get_lease("T1")
    assert lease is not None
    checks: list[bool] = []

    def lapses_after_the_first_check(_lease: object) -> bool:
        checks.append(True)
        return len(checks) == 1

    monkeypatch.setattr(Claims, "live", staticmethod(lapses_after_the_first_check))

    with pytest.raises(OperationError, match="holds no live lease"):
        if write == "check":
            claims.add_check("T1", "a.py:g", "t", token=lease.token)
        else:
            claims.add_note("T1", "x", token=lease.token)
    assert claims.nodes.get_checks("T1") == [] and claims.nodes.get_notes("T1") == []
