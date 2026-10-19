from pathlib import Path
from subprocess import CalledProcessError

import pytest
from lifecycle_estate import add, gated, git, make_estate, on_branch, stored

from taskmanager.core.enums import NodeKind
from taskmanager.core.models import Finding, Job
from taskmanager.core.status import JobKind, JobState, Outcome, Status
from taskmanager.engine import claims as claims_module
from taskmanager.engine import git as gitops
from taskmanager.engine.claims import Blocker, Claims, DecisionSpec
from taskmanager.engine.operations import OperationError


class FakeLanding:
    def start_land(self, node_id: str) -> str:
        return "job-1"


def merging_node(tmp_path: Path) -> tuple[Claims, Job]:
    claims = make_estate(tmp_path, config=gated("api"))
    add(claims, "T1", status=Status.REVIEWED, outcome=Outcome.APPROVE, review_cycles=1)
    claims.landing = FakeLanding()
    claims.start("T1", "merger", "s1")
    job = claims.jobs.create(Job(kind=JobKind.LAND, node_id="T1", repo="api", target="main"))
    return claims, job


def job_state(claims: Claims, job: Job) -> JobState:
    return next(j.state for j in claims.jobs.for_node("T1") if j.id == job.id)


def test_a_refused_blocked_release_leaves_the_landing_job_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    claims, job = merging_node(tmp_path)
    stopped: list[Job] = []
    monkeypatch.setattr(claims_module, "_stop_process", stopped.append)

    with pytest.raises(OperationError, match="--option takes"):
        claims.release("T1", Blocker(decision=DecisionSpec("which?", options=["bad"])))

    assert job_state(claims, job) == JobState.RUNNING
    assert stopped == []
    assert stored(claims, "T1").status == Status.MERGING
    assert claims.runtime.get_lease("T1") is not None


def test_a_release_that_commits_stops_the_landing_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    claims, job = merging_node(tmp_path)
    stopped: list[Job] = []
    monkeypatch.setattr(claims_module, "_stop_process", stopped.append)

    claims.release("T1", Blocker(decision=DecisionSpec("which?", options=["a|Option A"])))

    assert job_state(claims, job) == JobState.EXPIRED
    assert [j.id for j in stopped] == [job.id]
    assert stored(claims, "T1").status == Status.REVIEWED


def lapse_then_reclaim(claims: Claims, node_id: str) -> None:
    """The first agent's lease lapses and a second agent claims the node."""
    claims.runtime.release_lease(node_id)
    claims.start(node_id, "second", "s2")


def test_complete_after_the_lease_was_taken_by_another_claim_changes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    claims = make_estate(tmp_path, config=gated("api"))
    add(claims, "T1")
    first = claims.start("T1", "first", "s1")
    original = Claims._with_written_migrations

    def lapse(self: Claims, node):  # type: ignore[no-untyped-def]
        lapse_then_reclaim(claims, "T1")
        return original(self, node)

    monkeypatch.setattr(Claims, "_with_written_migrations", lapse)

    with pytest.raises(OperationError, match="claimed again"):
        claims.complete("T1", agent="first", token=first.token)

    lease = claims.runtime.get_lease("T1")
    assert lease is not None and lease.agent_id == "second"
    assert stored(claims, "T1").status == Status.IMPLEMENTING


def test_review_after_the_lease_was_taken_by_another_claim_changes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    claims = make_estate(tmp_path, config=gated("api"))
    add(claims, "T1", status=Status.IMPLEMENTED)
    first = claims.start("T1", "first", "s1")
    original = claims.nodes.get_findings
    fired: list[bool] = []

    def lapse(node_id: str) -> list[Finding]:
        if not fired:
            fired.append(True)
            lapse_then_reclaim(claims, "T1")
        return original(node_id)

    monkeypatch.setattr(claims.nodes, "get_findings", lapse)

    with pytest.raises(OperationError, match="claimed again"):
        claims.review("T1", approve=True, clean=True, agent="first", token=first.token)

    lease = claims.runtime.get_lease("T1")
    assert lease is not None and lease.agent_id == "second"
    assert stored(claims, "T1").status == Status.REVIEWING


def failed_node_with_worktree(tmp_path: Path, repos: tuple[str, ...] = ("api",)) -> Claims:
    claims = make_estate(tmp_path, repos=repos, config=gated(*repos))
    add(claims, "T1")
    started = claims.start("T1", "agent", "s1")
    assert started.worktree is not None
    claims.release("T1")
    claims.defer("T1", "later")
    return claims


def worktree_path(claims: Claims) -> Path:
    return claims.root / claims.config.worktree_dir / "api-T1"


def test_a_refused_reopen_on_a_new_branch_leaves_the_branch_and_worktree_in_place(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    claims = failed_node_with_worktree(tmp_path)

    def refuse(*_args: object, **_kwargs: object) -> Status:
        raise OperationError("refused", 409)

    monkeypatch.setattr(claims, "_rewrite", refuse)

    with pytest.raises(OperationError, match="refused"):
        claims.reopen("T1", "again", new_branch=True)

    api = claims.root / "api"
    assert git(api, "for-each-ref", "--format=%(refname:short)", "refs/heads/tm/T1*").split() == [
        "tm/T1"
    ]
    assert git(worktree_path(claims), "rev-parse", "--abbrev-ref", "HEAD") == "tm/T1"
    assert not Path(f"{worktree_path(claims)}@1").exists()


def test_a_reopen_whose_second_repository_cannot_retire_restores_the_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    claims = make_estate(tmp_path, repos=("api", "web"), config=gated("api", "web"))
    add(claims, "T1", status=Status.DEFERRED)
    for repo in ("api", "web"):
        on_branch(claims.root / repo, "tm/T1", "a.py", "a = 1\n")
    real = gitops.rename_branch

    def second_fails(repo: Path, old: str, new: str) -> None:
        if repo.name == "web" and "@" in new:
            raise CalledProcessError(1, "git branch -m", stderr="ref lock")
        real(repo, old, new)

    monkeypatch.setattr(gitops, "rename_branch", second_fails)

    with pytest.raises(OperationError, match="could not retire"):
        claims.reopen("T1", "again", new_branch=True)

    for repo in ("api", "web"):
        assert git(claims.root / repo, "branch", "--list", "tm/T1*").split() == ["tm/T1"]
    assert stored(claims, "T1").status == Status.DEFERRED


def test_a_failed_branch_rename_moves_the_worktree_back_and_a_retry_retires_it_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    claims = failed_node_with_worktree(tmp_path)
    real = gitops.rename_branch

    def locked(repo: Path, old: str, new: str) -> None:
        raise CalledProcessError(1, "git branch -m", stderr="ref lock")

    monkeypatch.setattr(gitops, "rename_branch", locked)
    with pytest.raises(OperationError, match="could not retire"):
        claims.reopen("T1", "again", new_branch=True)
    assert git(worktree_path(claims), "rev-parse", "--abbrev-ref", "HEAD") == "tm/T1"

    monkeypatch.setattr(gitops, "rename_branch", real)
    claims.reopen("T1", "again", new_branch=True)

    retired = Path(f"{worktree_path(claims)}@1")
    assert git(retired, "rev-parse", "--abbrev-ref", "HEAD") == "tm/T1@1"
    assert not Path(f"{retired}@1").exists()


def break_origin(claims: Claims, repo: str) -> None:
    git(claims.root / repo, "remote", "set-url", "origin", str(claims.root / "gone.git"))


def test_a_claim_is_refused_when_the_fetch_fails_and_only_a_stale_tracking_ref_remains(
    tmp_path: Path,
) -> None:
    claims = make_estate(tmp_path, config=gated("api"))
    add(claims, "T1")
    break_origin(claims, "api")

    with pytest.raises(OperationError, match="could not fetch main"):
        claims.start("T1", "agent", "s1")

    assert stored(claims, "T1").status == Status.READY
    assert git(claims.root / "api", "branch", "--list", "tm/T1") == ""


def test_a_reset_to_landed_is_refused_when_the_target_cannot_be_fetched(tmp_path: Path) -> None:
    claims = make_estate(tmp_path, config=gated("api"))
    add(claims, "T1")
    break_origin(claims, "api")

    with pytest.raises(OperationError, match="could not fetch main"):
        claims.reset("T1", Status.LANDED, "landed by hand")


def test_a_failed_fetch_of_a_branch_with_no_tracking_ref_is_not_stale(tmp_path: Path) -> None:
    claims = make_estate(tmp_path, config=gated("api"))

    claims._fetch("api", "origin", "tm/never-pushed", fresh=True)


def test_a_land_on_branch_the_remote_deleted_reads_as_the_default_branch(tmp_path: Path) -> None:
    claims = make_estate(tmp_path, config=gated("api"))
    add(claims, "S", NodeKind.SPEC)
    add(claims, "T1", parent="S")
    claims.ops.update_node("S", frontmatter_set={"land_on": "release/2"})
    api, origin = claims.root / "api", claims.root.parent / "origins" / "api.git"
    git(api, "push", "-q", "origin", "main:release/2")
    git(api, "fetch", "-q", "origin", "release/2")
    git(origin, "update-ref", "-d", "refs/heads/release/2")

    assert claims.fetched_top("T1", "api", fresh=True) == "main"
    assert git(api, "branch", "-r", "--list", "origin/release/2") == ""


def test_a_stale_ref_stays_refused_when_the_remote_cannot_say_whether_it_has_the_branch(
    tmp_path: Path,
) -> None:
    claims = make_estate(tmp_path, config=gated("api"))
    api = claims.root / "api"
    git(api, "push", "-q", "origin", "main:release/2")
    git(api, "fetch", "-q", "origin", "release/2")
    break_origin(claims, "api")

    with pytest.raises(OperationError, match="could not fetch release/2"):
        claims._fetch("api", "origin", "release/2", fresh=True)

    assert git(api, "branch", "-r", "--list", "origin/release/2").strip() == "origin/release/2"


def test_a_container_claim_refused_by_its_second_repository_cuts_no_worktree_in_the_first(
    tmp_path: Path,
) -> None:
    claims = make_estate(tmp_path, repos=("api", "web"), config=gated("api", "web"))
    add(claims, "P", NodeKind.PLAN)
    web = claims.root / "web"
    git(web, "remote", "set-url", "origin", str(claims.root / "gone.git"))
    git(web, "update-ref", "-d", "refs/remotes/origin/main")

    with pytest.raises(OperationError, match="web has no"):
        claims._cut(claims.node("P"), ["api", "web"], None, fix=False)

    assert not (claims.root / claims.config.worktree_dir / "P" / "api").exists()
    assert git(claims.root / "api", "branch", "--list", "tm/P") == ""


def test_complete_after_the_lease_lapsed_without_a_new_claim_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    claims = make_estate(tmp_path, config=gated("api"))
    add(claims, "T1")
    first = claims.start("T1", "first", "s1")
    checks: list[bool] = []

    def lapses_after_the_first_check(_lease: object) -> bool:
        checks.append(True)
        return len(checks) == 1

    monkeypatch.setattr(Claims, "live", staticmethod(lapses_after_the_first_check))

    with pytest.raises(OperationError, match="holds no live lease"):
        claims.complete("T1", token=first.token)

    assert stored(claims, "T1").status == Status.IMPLEMENTING


def test_complete_after_the_lease_was_swept_without_a_new_claim_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    claims = make_estate(tmp_path, config=gated("api"))
    add(claims, "T1")
    first = claims.start("T1", "first", "s1")
    written = Claims._with_written_migrations

    def swept(self: Claims, node):  # type: ignore[no-untyped-def]
        self.runtime.release_lease("T1")
        return written(self, node)

    monkeypatch.setattr(Claims, "_with_written_migrations", swept)

    with pytest.raises(OperationError, match="holds no lease: it was swept or released"):
        claims.complete("T1", agent="first", token=first.token)


def test_only_a_claim_needs_the_top_branch_fetched_fresh(tmp_path: Path) -> None:
    claims = make_estate(tmp_path, config=gated("api"))
    add(claims, "S", NodeKind.SPEC)
    add(claims, "T1", parent="S")
    claims.ops.update_node("S", frontmatter_set={"land_on": "release/2"})
    break_origin(claims, "api")

    assert claims.fetched_top("T1", "api") == "main"
    with pytest.raises(OperationError, match="could not fetch main"):
        claims.fetched_top("T1", "api", fresh=True)


def test_a_note_keeps_the_custom_header_of_the_section_it_appends_to(tmp_path: Path) -> None:
    claims = make_estate(tmp_path, config=gated("api"))
    add(claims, "T1")
    claims.ops.set_section("T1", "merge", "first", header="## Landing log")

    claims.note("T1", "merge", "second")

    found = claims.nodes.get_section("T1", "merge")
    assert found is not None
    assert found.header == "## Landing log"
    assert found.content.startswith("first\n\n") and found.content.endswith("second")
