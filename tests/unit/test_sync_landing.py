import time
from pathlib import Path

from lifecycle_estate import (
    add,
    attach_landing,
    branch_at,
    commit,
    git,
    junit_gate,
    make_estate,
    on_branch,
    push_main,
    stored,
)

from taskmanager.core.enums import NodeKind
from taskmanager.core.status import Action, JobState, Merge, Outcome, Status
from taskmanager.engine.claims import Claims
from taskmanager.engine.config import Gate, ProjectConfig, RepoConfig
from taskmanager.engine.landing import Landing

TRUE = Gate(command="true", junit=None, timeout=60)


def lagging_parent(tmp_path: Path, config: ProjectConfig | None = None) -> tuple[Claims, Landing]:
    """X builds on tm/P and depends on Y, which landed on main after tm/P was cut."""
    claims = make_estate(tmp_path, config=config)
    api = claims.root / "api"
    add(claims, "P", NodeKind.PLAN, review=True, fix=True)
    add(claims, "Y", status=Status.COMPLETED)
    add(claims, "X", parent="P", merge=Merge.PARENT, depends=("Y",))
    branch_at(api, "tm/P")
    push_main(api, "y.py", "y = 1\n")
    return claims, attach_landing(claims)


def test_a_claim_on_a_lagging_parent_branch_syncs_main_in_before_implement_starts(
    tmp_path: Path,
) -> None:
    claims, landing = lagging_parent(tmp_path)
    api = claims.root / "api"

    first = claims.start("X", "implementer", "s1")

    assert first.action == Action.BLOCKED
    assert first.reason == "syncing tm/P"
    assert first.job is not None and first.job.startswith("sync-")
    held = stored(claims, "X")
    assert (held.status, held.claimed_from) == (Status.READY, None)
    lease = claims.runtime.get_lease("X")
    assert lease is not None and lease.action == Action.SYNC

    assert landing.run(first.job) == JobState.SUCCEEDED
    git(api, "merge-base", "--is-ancestor", "origin/main", "tm/P")
    assert claims.runtime.get_lease("X") is None
    assert stored(claims, "X").claimed_from is None

    second = claims.start("X", "implementer", "s1")
    assert second.action == Action.IMPLEMENT
    assert second.worktree is not None
    assert (Path(second.worktree) / "y.py").exists()


def test_a_sync_conflict_is_handed_to_an_agent_who_resolves_and_resumes(tmp_path: Path) -> None:
    claims = make_estate(tmp_path)
    api = claims.root / "api"
    add(claims, "P", NodeKind.PLAN, review=True, fix=True)
    add(claims, "Y", status=Status.COMPLETED)
    add(claims, "X", parent="P", merge=Merge.PARENT, depends=("Y",))
    on_branch(api, "tm/P", "app.py", "parent\n")
    push_main(api, "app.py", "main\n")
    landing = attach_landing(claims)
    first = claims.start("X", "implementer", "s1")
    assert first.job is not None
    assert landing.run(first.job) == JobState.NEEDS_AGENT

    handed = claims.start("X", "resolver", "s2")

    assert (handed.action, handed.job, handed.branch) == (Action.SYNC, first.job, "tm/P")
    assert handed.worktree is not None
    worktree = Path(handed.worktree)
    (worktree / "app.py").write_text("both\n")
    git(worktree, "add", "app.py")
    git(worktree, "commit", "-q", "--no-edit")
    assert landing.resume(first.job) == JobState.SUCCEEDED
    git(api, "merge-base", "--is-ancestor", "origin/main", "tm/P")
    assert git(api, "show", "tm/P:app.py") == "both"
    assert claims.start("X", "implementer", "s1").action == Action.IMPLEMENT


def test_a_red_parent_gate_stops_a_sync_for_an_agent(tmp_path: Path) -> None:
    red = ProjectConfig(
        repos={"api": RepoConfig(gates={"parent": Gate(command="exit 1", junit=None, timeout=60)})}
    )
    claims, landing = lagging_parent(tmp_path, red)
    first = claims.start("X", "implementer", "s1")
    assert first.job is not None

    assert landing.run(first.job) == JobState.NEEDS_AGENT
    job = claims.jobs.get(first.job)
    assert job is not None and job.result["reason"] == "red"


def test_a_sync_its_agent_abandons_counts_one_step_failure(tmp_path: Path) -> None:
    claims = make_estate(tmp_path)
    api = claims.root / "api"
    add(claims, "P", NodeKind.PLAN, review=True, fix=True)
    add(claims, "Y", status=Status.COMPLETED)
    add(claims, "X", parent="P", merge=Merge.PARENT, depends=("Y",))
    on_branch(api, "tm/P", "app.py", "parent\n")
    push_main(api, "app.py", "main\n")
    landing = attach_landing(claims)
    first = claims.start("X", "implementer", "s1")
    assert first.job is not None
    landing.run(first.job)
    assert claims.start("X", "resolver", "s2", ttl=1).action == Action.SYNC
    time.sleep(1.5)

    assert claims.sweep() == ["X"]

    job = claims.jobs.get(first.job)
    assert job is not None and job.state == JobState.EXPIRED
    node = stored(claims, "X")
    assert (node.status, node.step_failures, node.claimed_from) == (Status.READY, 1, None)
    assert claims.runtime.get_lease("X") is None


def two_repo_container(tmp_path: Path, **columns: object) -> tuple[Claims, Landing]:
    config = ProjectConfig(
        repo_order=["api", "web"],
        repos={
            "api": RepoConfig(gates={"main": TRUE}),
            "web": RepoConfig(gates={"main": junit_gate(tmp_path)}),
        },
    )
    claims = make_estate(tmp_path, repos=("api", "web"), config=config)
    add(
        claims,
        "P",
        NodeKind.PLAN,
        review=True,
        fix=True,
        status=Status.REVIEWED,
        outcome=Outcome.APPROVE,
        review_cycles=1,
        **columns,
    )
    add(claims, "A", parent="P", repo="api", merge=Merge.PARENT, status=Status.COMPLETED)
    add(claims, "B", parent="P", repo="web", merge=Merge.PARENT, status=Status.COMPLETED)
    return claims, attach_landing(claims)


def test_a_container_lands_each_repository_in_order_and_only_what_is_left_after_a_failure(
    tmp_path: Path,
) -> None:
    claims, landing = two_repo_container(tmp_path)
    api, web = claims.root / "api", claims.root / "web"
    on_branch(api, "tm/P", "a.py", "a = 1\n")
    on_branch(web, "tm/P", "b.py", "b = 1\n")
    on_branch(web, "tm/P", "failing.txt", "broken\n")

    merge = claims.start("P", "merger", "s1")
    assert merge.job is not None
    assert landing.run(merge.job) == JobState.OWN_DEFECT

    node = stored(claims, "P")
    assert (node.status, node.outcome, node.merge_attempts) == (
        Status.REVIEWED,
        Outcome.MERGE_FAILED,
        1,
    )
    assert git(api, "show", "origin/main:a.py") == "a = 1"
    assert "b.py" not in git(web, "ls-tree", "--name-only", "origin/main").split()

    fix = claims.start("P", "fixer", "s1")
    assert fix.action == Action.FIX
    assert fix.worktree is not None
    assert fix.worktrees == {
        "api": str(Path(fix.worktree) / "api"),
        "web": str(Path(fix.worktree) / "web"),
    }
    commit(Path(fix.worktrees["web"]), "failing.txt", None, "web: gate green again")
    assert claims.complete("P") == Status.FIXED
    assert claims.start("P", "reviewer", "s1").action == Action.REVIEW
    claims.ops.set_section("P", "review", "the web gate is green")
    assert claims.review("P", approve=True) == Status.REVIEWED
    assert stored(claims, "P").review_cycles == 1

    again = claims.start("P", "merger", "s1")
    assert again.job is not None
    assert landing.run(again.job) == JobState.SUCCEEDED

    assert stored(claims, "P").status == Status.COMPLETED
    git(api, "fetch", "-q", "origin")
    subjects = git(api, "log", "--first-parent", "--format=%s", "origin/main").splitlines()
    assert subjects.count("merge(P): land tm/P on main") == 1
    assert git(web, "show", "origin/main:b.py") == "b = 1"
    first_job = claims.jobs.get(again.job)
    assert first_job is not None and first_job.result["next"]


def test_land_order_overrides_the_configured_repository_order(tmp_path: Path) -> None:
    claims, _ = two_repo_container(tmp_path, land_order=["web", "api"])

    merge = claims.start("P", "merger", "s1")

    assert merge.job is not None
    job = claims.jobs.get(merge.job)
    assert job is not None and job.repo == "web"
