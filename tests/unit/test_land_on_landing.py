"""A landing at the top of its chain merges, gates, pushes, parks, proves and syncs against its
chain's target branch on origin, wherever that is; `tm verify run` reads it by default."""

import json
import subprocess
import time
from pathlib import Path

import pytest
from lifecycle_estate import (
    add,
    attach_landing,
    branch_at,
    git,
    junit_gate,
    make_estate,
    on_branch,
    push_main,
    stored,
)
from typer.testing import CliRunner

from taskmanager.cli.main import app as cli_app
from taskmanager.core.enums import NodeKind, VerificationType
from taskmanager.core.models import Job, NodeVerification
from taskmanager.core.status import Action, JobKind, JobState, Merge, Outcome, Status
from taskmanager.engine import git as gitops
from taskmanager.engine.claims import Claims
from taskmanager.engine.config import Gate, ProjectConfig, RepoConfig
from taskmanager.engine.landing import Landing
from taskmanager.engine.operations import OperationError

RELEASE = "release/x"
TRUE = Gate(command="true", junit=None, timeout=60)


def push_branch(repo: Path, branch: str, path: str, content: str | None) -> str:
    """Commits to the local `branch` (cut from origin/main when new) and pushes it to origin."""
    sha = on_branch(repo, branch, path, content)
    git(repo, "push", "-q", "origin", f"{branch}:{branch}")
    git(repo, "fetch", "-q", "origin")
    return sha


def released(tmp_path: Path, gate: Gate | None = TRUE) -> tuple[Claims, Landing]:
    """Spec S lands on release/x. A condition's result is cached for a second only."""
    repos = {"api": RepoConfig(gates={"main": gate})} if gate is not None else {}
    claims = make_estate(tmp_path, config=ProjectConfig(repos=repos, condition_ttl=1))
    add(claims, "S", NodeKind.SPEC)
    claims.ops.update_node("S", frontmatter_set={"land_on": RELEASE})
    return claims, attach_landing(claims)


def reviewed(claims: Claims, node_id: str, content: str | None, base: str = "origin/main") -> None:
    """A reviewed task under S; None leaves its branch at `base` with nothing of its own."""
    add(
        claims,
        node_id,
        parent="S",
        status=Status.REVIEWED,
        outcome=Outcome.APPROVE,
        review_cycles=1,
    )
    api = claims.root / "api"
    if content is None:
        branch_at(api, f"tm/{node_id}", base)
    else:
        on_branch(api, f"tm/{node_id}", "feature.py", content, base=base)


def land(claims: Claims, landing: Landing, node_id: str) -> tuple[Job, JobState]:
    result = claims.start(node_id, "merger", "s1")
    assert result.action == Action.MERGE, result.reason
    assert result.job is not None
    state = landing.run(result.job)
    job = claims.jobs.get(result.job)
    assert job is not None
    return job, state


def remote_head(repo: Path, branch: str) -> str:
    return git(repo, "ls-remote", "origin", f"refs/heads/{branch}")


def red_targets(claims: Claims, node_id: str) -> list[str]:
    return [c.command for c in claims.nodes.get_conditions(node_id) if c.command]


def exits(command: str) -> int:
    return subprocess.run(command, shell=True, capture_output=True, check=False).returncode


def test_a_top_landing_gates_with_gates_main_and_pushes_its_spec_s_target_leaving_main_alone(
    tmp_path: Path,
) -> None:
    log = tmp_path / "gate.log"
    claims, landing = released(tmp_path, Gate(command=f"echo {{target}} >> {log}", timeout=60))
    api = claims.root / "api"
    push_branch(api, RELEASE, "release.txt", "r\n")
    main = remote_head(api, "main")
    reviewed(claims, "T1", "x = 1\n", base=f"origin/{RELEASE}")

    job, state = land(claims, landing, "T1")

    assert (state, job.target) == (JobState.SUCCEEDED, RELEASE)
    assert stored(claims, "T1").status == Status.COMPLETED
    assert git(api, "log", "-1", "--format=%s", f"origin/{RELEASE}") == (
        f"merge(T1): land tm/T1 on {RELEASE}"
    )
    assert git(api, "show", f"origin/{RELEASE}:feature.py") == "x = 1"
    assert log.read_text().split() == [RELEASE]
    assert remote_head(api, "main") == main


@pytest.mark.parametrize("content", ["x = 1\n", None], ids=["with-a-change", "with-nothing-new"])
def test_a_top_landing_on_a_target_origin_lacks_creates_it_from_the_default_branch(
    tmp_path: Path, content: str | None
) -> None:
    claims, landing = released(tmp_path)
    api = claims.root / "api"
    main = remote_head(api, "main")
    reviewed(claims, "T1", content)

    assert land(claims, landing, "T1")[1] == JobState.SUCCEEDED

    assert remote_head(api, RELEASE)
    git(api, "merge-base", "--is-ancestor", "origin/main", f"origin/{RELEASE}")
    assert git(api, "ls-tree", "--name-only", f"origin/{RELEASE}").split() == (
        ["README.md", "feature.py"] if content else ["README.md"]
    )
    assert remote_head(api, "main") == main


class FakeRemote:
    """`origin` holding `head` for every branch ("" for a branch it does not have)."""

    def __init__(self, head: str) -> None:
        self.head = head
        self.read: list[str] = []
        self.pushed: list[str] = []
        self.merged: list[str] = []

    def ls_remote(self, _repo: Path, ref: str) -> tuple[str, subprocess.CompletedProcess[str]]:
        self.read.append(ref)
        return self.head, subprocess.CompletedProcess(["git", "ls-remote"], 0, "", "")

    def push(self, _worktree: Path, target: str) -> subprocess.CompletedProcess[str]:
        self.pushed.append(target)
        return subprocess.CompletedProcess(["git", "push"], 0, "", "")

    def merge_no_ff(self, _worktree: Path, ref: str, _subject: str) -> bool:
        self.merged.append(ref)
        return True


@pytest.mark.parametrize(
    ("head", "step", "pushed", "merged"),
    [
        ("base", "verify", [RELEASE], []),
        ("", "verify", [RELEASE], []),
        ("moved", "gate", [], [f"origin/{RELEASE}"]),
    ],
    ids=["unmoved", "not-on-origin-yet", "moved"],
)
def test_a_top_push_reads_moves_or_creates_the_target_on_origin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    head: str,
    step: str,
    pushed: list[str],
    merged: list[str],
) -> None:
    claims, landing = released(tmp_path)
    add(claims, "T1", parent="S", status=Status.MERGING, claimed_from=Status.REVIEWED)
    fake = FakeRemote(head)
    monkeypatch.setattr(gitops, "ls_remote", fake.ls_remote)
    monkeypatch.setattr(gitops, "push", fake.push)
    monkeypatch.setattr(gitops, "merge_no_ff", fake.merge_no_ff)
    monkeypatch.setattr(gitops, "fetch", lambda repo, branch: True)
    monkeypatch.setattr(gitops, "rev_parse", lambda repo, ref: "moved")
    job = claims.jobs.create(
        Job(
            kind=JobKind.LAND,
            node_id="T1",
            repo="api",
            target=RELEASE,
            step="push",
            worktree=str(claims.root / "land"),
            result={"base_sha": "base"},
        )
    )

    assert landing._push(job) == step

    assert fake.read == [f"refs/heads/{RELEASE}"]
    assert (fake.pushed, fake.merged) == (pushed, merged)


def test_a_top_landing_with_no_main_gate_stops_naming_its_target(tmp_path: Path) -> None:
    claims, landing = released(tmp_path, gate=None)
    reviewed(claims, "T1", "x = 1\n")

    job, state = land(claims, landing, "T1")

    assert (state, job.result["reason"]) == (JobState.NEEDS_AGENT, "no gate")
    assert job.result["detail"].endswith(f"cannot land on {RELEASE}")


def test_a_landing_parked_on_a_red_target_reads_it_on_origin_and_lands_once_it_moves(
    tmp_path: Path,
) -> None:
    claims, landing = released(tmp_path, junit_gate(tmp_path))
    api = claims.root / "api"
    push_branch(api, RELEASE, "failing.txt", "a\n")
    reviewed(claims, "T1", "x = 1\n", base=f"origin/{RELEASE}")

    job, state = land(claims, landing, "T1")

    assert state == JobState.CONDITION_UNMET
    assert job.result["red_target"]["target"] == RELEASE
    [command] = red_targets(claims, "T1")
    assert f"--target {RELEASE} --remote" in command
    assert exits(command) == 1
    on_branch(api, RELEASE, "failing.txt", None)
    assert exits(command) == 1
    git(api, "push", "-q", "origin", f"{RELEASE}:{RELEASE}")
    assert exits(command) == 0

    time.sleep(1.1)
    assert land(claims, landing, "T1")[1] == JobState.SUCCEEDED
    assert git(api, "show", f"origin/{RELEASE}:feature.py") == "x = 1"


def test_a_landing_creating_its_target_parks_on_the_red_default_branch_it_merged_from(
    tmp_path: Path,
) -> None:
    claims, landing = released(tmp_path, junit_gate(tmp_path))
    api = claims.root / "api"
    push_main(api, "failing.txt", "a\n")
    reviewed(claims, "T1", "x = 1\n")

    job, state = land(claims, landing, "T1")

    assert state == JobState.CONDITION_UNMET
    assert job.result["red_target"]["target"] == "main"
    [command] = red_targets(claims, "T1")
    assert "--target main --remote" in command
    assert exits(command) == 1
    push_main(api, "failing.txt", None)
    assert exits(command) == 0
    assert not remote_head(api, RELEASE)


def two_defaults(tmp_path: Path) -> tuple[Claims, Landing]:
    """Plan P, with no spec, holds A in `api` (default branch trunk) and W in `web` (main)."""
    config = ProjectConfig(
        repo_order=["api", "web"],
        repos={
            "api": RepoConfig(default_branch="trunk", gates={"main": TRUE}),
            "web": RepoConfig(gates={"main": TRUE}),
        },
    )
    claims = make_estate(tmp_path, repos=("api", "web"), config=config)
    push_branch(claims.root / "api", "trunk", "trunk.txt", "t\n")
    add(
        claims,
        "P",
        NodeKind.PLAN,
        review=True,
        fix=True,
        status=Status.REVIEWED,
        outcome=Outcome.APPROVE,
        review_cycles=1,
    )
    add(claims, "A", parent="P", repo="api", merge=Merge.PARENT, status=Status.COMPLETED)
    add(claims, "W", parent="P", repo="web", merge=Merge.PARENT, status=Status.COMPLETED)
    return claims, attach_landing(claims)


def test_a_container_lands_in_each_repository_on_that_repository_s_default_branch(
    tmp_path: Path,
) -> None:
    claims, landing = two_defaults(tmp_path)
    api, web = claims.root / "api", claims.root / "web"
    on_branch(api, "tm/P", "a.py", "a\n", base="origin/trunk")
    on_branch(web, "tm/P", "w.py", "w\n")

    job, state = land(claims, landing, "P")

    assert state == JobState.SUCCEEDED
    following = claims.jobs.get(str(job.result["next"]))
    assert following is not None
    assert (job.repo, job.target, following.repo, following.target) == (
        "api",
        "trunk",
        "web",
        "main",
    )
    assert git(api, "log", "-1", "--format=%s", "origin/trunk") == "merge(P): land tm/P on trunk"
    assert git(web, "log", "-1", "--format=%s", "origin/main") == "merge(P): land tm/P on main"
    assert not remote_head(web, "trunk")


def test_a_container_s_landing_is_proved_and_its_empty_diff_read_per_repository_target(
    tmp_path: Path,
) -> None:
    claims, _ = two_defaults(tmp_path)
    api, web = claims.root / "api", claims.root / "web"
    branch_at(api, "tm/P", "origin/trunk")
    branch_at(web, "tm/P")

    assert claims.ops.nothing_to_land("P")

    on_branch(web, "tm/P", "w.py", "w\n")
    assert not claims.ops.nothing_to_land("P")
    with pytest.raises(OperationError, match="tm/P is not on main in web: land it first"):
        claims.reset("P", Status.LANDED, "landed by hand")
    git(web, "push", "-q", "origin", "tm/P:main")
    assert claims.reset("P", Status.LANDED, "landed by hand") == Status.LANDED


def test_a_sync_from_the_top_reads_each_repository_s_own_target(tmp_path: Path) -> None:
    claims, landing = two_defaults(tmp_path)
    api, web = claims.root / "api", claims.root / "web"
    add(claims, "Q", NodeKind.PLAN, review=True, fix=True, status=Status.COMPLETED)
    for task, repo in (("QA", "api"), ("QW", "web")):
        add(claims, task, parent="Q", repo=repo, merge=Merge.PARENT, status=Status.COMPLETED)
    add(claims, "X", parent="P", repo="api", merge=Merge.PARENT, depends=("Q",))
    branch_at(api, "tm/P", "origin/trunk")
    branch_at(web, "tm/P")
    on_branch(api, "tm/Q", "q.py", "q\n", base="origin/trunk")
    on_branch(web, "tm/Q", "q.py", "q\n")
    git(api, "push", "-q", "origin", "tm/Q:trunk")
    git(web, "push", "-q", "origin", "tm/Q:main")

    first = claims.start("X", "implementer", "s1")

    assert (first.action, first.reason) == (Action.BLOCKED, "syncing tm/P")
    assert first.job is not None
    job = claims.jobs.get(first.job)
    assert job is not None
    assert job.result["units"] == [["origin/trunk", "tm/P", "api"], ["origin/main", "tm/P", "web"]]
    assert landing.run(first.job) == JobState.SUCCEEDED
    assert git(api, "show", "tm/P:q.py") == git(web, "show", "tm/P:q.py") == "q"


def test_a_review_of_a_plan_landed_on_its_target_reads_that_target_on_origin(
    tmp_path: Path,
) -> None:
    claims, _ = released(tmp_path)
    push_branch(claims.root / "api", RELEASE, "landed.txt", "landed\n")
    add(claims, "P", NodeKind.PLAN, parent="S", review=True, status=Status.LANDED)
    add(claims, "A", parent="P", review=False, fix=False, status=Status.COMPLETED)

    result = claims.start("P", "reviewer", "s1")

    assert (result.action, result.branch, result.base) == (
        Action.REVIEW,
        f"origin/{RELEASE}",
        RELEASE,
    )


def test_a_review_of_a_container_landed_across_targets_names_each_repository_s_own(
    tmp_path: Path,
) -> None:
    """Plan P landed on api's trunk and web's main, web landing first."""
    config = ProjectConfig(
        repo_order=["web", "api"], repos={"api": RepoConfig(default_branch="trunk")}
    )
    claims = make_estate(tmp_path, repos=("api", "web"), config=config)
    push_branch(claims.root / "api", "trunk", "trunk.txt", "t\n")
    add(claims, "P", NodeKind.PLAN, review=True, fix=True, status=Status.LANDED)
    add(claims, "A", parent="P", repo="api", merge=Merge.PARENT, status=Status.COMPLETED)
    add(claims, "W", parent="P", repo="web", merge=Merge.PARENT, status=Status.COMPLETED)

    res = CliRunner().invoke(
        cli_app,
        ["task", "start", "P", "--agent", "r", "--session", "s1", "--json", "-C", str(claims.root)],
    )

    assert res.exit_code == 0, res.output
    claim = json.loads(res.output)
    assert (claim["action"], claim["repos"]) == ("review", ["web", "api"])
    assert (claim["branch"], claim["base"]) == ("origin/main", "main")
    assert claim["bases"] == {"web": "main", "api": "trunk"}


def test_verify_run_with_no_ref_fetches_and_reads_each_task_s_target_on_origin(
    tmp_path: Path,
) -> None:
    claims, _ = released(tmp_path)
    api = claims.root / "api"
    push_branch(api, RELEASE, "release.txt", "r\n")
    git(api, "update-ref", "-d", f"refs/remotes/origin/{RELEASE}")
    add(claims, "T1", parent="S")
    for kind, target, pattern in (
        (VerificationType.FILE_EXISTS, "release.txt", None),
        (VerificationType.TEST_COMMAND, "", f'test "$TM_VERIFY_REF" = origin/{RELEASE}'),
    ):
        claims.nodes.add_verification(
            NodeVerification(
                node_id="T1",
                verification_type=kind,
                target_path=target,
                expected_pattern=pattern,
            )
        )

    passed, results = claims.ops.run_verifications("T1")

    assert passed, [r.message for r in results]
    assert f"git ref origin/{RELEASE} in api" in results[0].message
