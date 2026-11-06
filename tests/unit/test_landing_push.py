import json
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest
from lifecycle_estate import add, attach_landing, git, make_estate, on_branch, push_main, stored
from typer.testing import CliRunner

from taskmanager.cli.main import app
from taskmanager.core.enums import NodeKind
from taskmanager.core.models import Job
from taskmanager.core.status import Action, JobKind, JobState, Merge, Outcome, Status
from taskmanager.db.cache_repo import CacheRepository
from taskmanager.engine import git as gitops
from taskmanager.engine.claims import Claims
from taskmanager.engine.config import Gate, ProjectConfig, RepoConfig
from taskmanager.engine.landing import PUSH_ERROR_LINES, PUSH_PAUSE_SECONDS, Landing

UNREACHABLE = "fatal: unable to access 'origin': Could not resolve host: example.test"
REFUSED = (
    "remote: error: GH006: Protected branch update failed for refs/heads/main.\n"
    "remote: error: Changes must be made through a pull request.\n"
    "To example.test:org/api.git\n"
    " ! [remote rejected] HEAD -> main (protected branch hook declined)\n"
    "error: failed to push some refs to 'example.test:org/api.git'\n"
    "hint: a line past the recorded head\n"
    "hint: another\n"
)


def ran(*args: str, code: int = 0, stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(["git", *args], code, "", stderr)


class FakeRemote:
    def __init__(self, heads: list[str], refusals: int) -> None:
        self._heads: Iterator[str] = iter(heads)
        self.refusals = refusals
        self.pushes: list[str] = []

    def ls_remote(
        self, _repo: Path, _remote: str | None, ref: str
    ) -> tuple[str, subprocess.CompletedProcess[str]]:
        head = next(self._heads)
        if head:
            return head, ran("ls-remote", "origin", ref)
        return "", ran("ls-remote", "origin", ref, code=128, stderr=UNREACHABLE)

    def push(
        self, _worktree: Path, _remote: str | None, target: str, _lease: str | None = None
    ) -> subprocess.CompletedProcess[str]:
        self.pushes.append(target)
        if self.refusals:
            self.refusals -= 1
            return ran("push", "-q", "origin", f"HEAD:refs/heads/{target}", code=1, stderr=REFUSED)
        return ran("push", "-q", "origin", f"HEAD:refs/heads/{target}")


def estate(tmp_path: Path) -> tuple[Claims, Landing, list[float]]:
    config = ProjectConfig(
        repos={"api": RepoConfig(gates={"main": Gate(command="true")}, push=True)}
    )
    claims = make_estate(tmp_path, config=config)
    add(claims, "T1", status=Status.MERGING, claimed_from=Status.REVIEWED)
    pauses: list[float] = []
    landing = Landing(
        claims.root,
        claims.config,
        claims,
        CacheRepository(claims.nodes.db),
        claims.jobs,
        detach=False,
        sleep=pauses.append,
    )
    return claims, landing, pauses


def pushing(claims: Claims) -> Job:
    return claims.jobs.create(
        Job(
            kind=JobKind.LAND,
            node_id="T1",
            repo="api",
            target="main",
            step="push",
            worktree=str(claims.root / "land"),
            result={"base_sha": "base"},
        )
    )


def remote(monkeypatch: pytest.MonkeyPatch, heads: list[str], refusals: int = 0) -> FakeRemote:
    fake = FakeRemote(heads, refusals)
    monkeypatch.setattr(gitops, "ls_remote", fake.ls_remote)
    monkeypatch.setattr(gitops, "push", fake.push)
    return fake


def test_push_after_two_unanswered_ls_remotes_pushes_on_the_third_try_and_records_both(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    claims, landing, pauses = estate(tmp_path)
    fake = remote(monkeypatch, ["", "", "base"])
    job = pushing(claims)

    assert landing._push(job) == "verify"

    assert fake.pushes == ["main"]
    assert pauses == [PUSH_PAUSE_SECONDS, 2 * PUSH_PAUSE_SECONDS]
    errors = job.result["push_errors"]
    assert [e["exit_code"] for e in errors] == [128, 128]
    assert all(e["command"] == "git ls-remote origin refs/heads/main" for e in errors)
    assert all(e["stderr"] == UNREACHABLE for e in errors)


def test_push_refused_three_times_stops_at_push_failed_with_each_stderr_head(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    claims, landing, pauses = estate(tmp_path)
    fake = remote(monkeypatch, ["base"] * 3, refusals=3)
    job = pushing(claims)

    assert landing._push(job) == JobState.NEEDS_AGENT

    assert fake.pushes == ["main"] * 3
    assert pauses == [PUSH_PAUSE_SECONDS, 2 * PUSH_PAUSE_SECONDS]
    stored = claims.jobs.get(job.id)
    assert stored is not None
    assert stored.result["reason"] == "push_failed"
    head = "\n".join(REFUSED.splitlines()[:PUSH_ERROR_LINES])
    assert (
        stored.result["push_errors"]
        == [{"command": "git push -q origin HEAD:refs/heads/main", "exit_code": 1, "stderr": head}]
        * 3
    )


def test_push_to_a_moved_main_merges_it_in_and_gates_again_without_pushing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    claims, landing, pauses = estate(tmp_path)
    fake = remote(monkeypatch, ["moved"])
    merged: list[str] = []

    def merge_no_ff(_worktree: Path, ref: str, _subject: str) -> bool:
        merged.append(ref)
        return True

    monkeypatch.setattr(gitops, "fetch", lambda repo, remote, branch: True)
    monkeypatch.setattr(gitops, "merge_no_ff", merge_no_ff)
    monkeypatch.setattr(gitops, "rev_parse", lambda repo, ref: "moved")
    job = pushing(claims)

    assert landing._push(job) == "gate"

    assert merged == ["origin/main"]
    assert job.result["base_sha"] == "moved"
    assert not fake.pushes
    assert not pauses
    assert "push_errors" not in job.result


def test_job_status_of_a_landing_stopped_at_push_prints_its_push_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    claims, landing, _ = estate(tmp_path)
    remote(monkeypatch, ["", "", ""])
    job = pushing(claims)
    assert landing._push(job) == JobState.NEEDS_AGENT

    res = CliRunner().invoke(app, ["job", "status", job.id, "--path", str(claims.root)])

    assert res.exit_code == 0, res.stdout
    printed = json.loads(res.stdout)["result"]
    assert printed["reason"] == "push_failed"
    assert [e["stderr"] for e in printed["push_errors"]] == [UNREACHABLE] * 3


def conflicting(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gitops, "fetch", lambda repo, remote, branch: True)
    monkeypatch.setattr(gitops, "merge_no_ff", lambda _worktree, _ref, _subject: False)
    monkeypatch.setattr(gitops, "rev_parse", lambda repo, ref: "moved")


def test_push_to_a_moved_main_that_conflicts_parks_with_the_moved_sha_as_the_baseline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    claims, landing, _ = estate(tmp_path)
    remote(monkeypatch, ["moved"])
    conflicting(monkeypatch)
    job = pushing(claims)

    assert landing._push(job) == JobState.NEEDS_AGENT

    stored = claims.jobs.get(job.id)
    assert stored is not None
    assert stored.result["reason"] == "conflict"
    assert stored.result["base_sha"] == "moved"


def test_a_container_branch_that_moved_and_conflicts_parks_with_the_moved_sha_as_the_baseline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    claims, landing, _ = estate(tmp_path)
    conflicting(monkeypatch)
    monkeypatch.setattr(gitops, "update_ref_cas", lambda repo, ref, new, old: False)
    job = pushing(claims)

    assert landing._move_branch(job, Path(job.worktree or ""), "done") == JobState.NEEDS_AGENT

    stored = claims.jobs.get(job.id)
    assert stored is not None
    assert stored.result["reason"] == "conflict"
    assert stored.result["base_sha"] == "moved"


RELEASE = "release/x"


def spec_estate(tmp_path: Path, **repo: object) -> Claims:
    """Spec S lands on release/x; task T1, reviewed, is the only task under it."""
    repos = {"api": RepoConfig(gates={"main": Gate(command="true", timeout=60)}, **repo)}
    claims = make_estate(tmp_path, config=ProjectConfig(repos=repos))
    add(claims, "S", NodeKind.SPEC)
    claims.ops.update_node("S", frontmatter_set={"land_on": RELEASE})
    add(
        claims,
        "T1",
        parent="S",
        status=Status.REVIEWED,
        outcome=Outcome.APPROVE,
        review_cycles=1,
    )
    return claims


def land_t1(claims: Claims) -> tuple[Job, JobState]:
    landing = attach_landing(claims)
    result = claims.start("T1", "merger", "s1")
    assert result.action == Action.MERGE, result.reason
    assert result.job is not None
    state = landing.run(result.job)
    job = claims.jobs.get(result.job)
    assert job is not None
    return job, state


def seed_origin_release(api: Path) -> str:
    """origin holds release/x with one commit and the clone has no such local branch."""
    on_branch(api, RELEASE, "release.txt", "r\n")
    git(api, "push", "-q", "origin", f"{RELEASE}:{RELEASE}")
    git(api, "branch", "-q", "-D", RELEASE)
    git(api, "fetch", "-q", "origin")
    return git(api, "rev-parse", f"origin/{RELEASE}")


def test_a_container_lands_locally_when_push_is_off(tmp_path: Path) -> None:
    claims = spec_estate(tmp_path)
    api = claims.root / "api"
    on_branch(api, "tm/T1", "feature.py", "x = 1\n")

    assert land_t1(claims)[1] == JobState.SUCCEEDED

    assert git(api, "ls-remote", "origin", f"refs/heads/{RELEASE}") == ""
    assert git(api, "show", f"refs/heads/{RELEASE}:feature.py") == "x = 1"
    assert stored(claims, "T1").status == Status.COMPLETED


def test_after_land_still_runs_when_push_is_off(tmp_path: Path) -> None:
    marker = tmp_path / "hook.ran"
    claims = spec_estate(tmp_path, after_land=f"touch {marker}")
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
    add(claims, "A", parent="P", merge=Merge.PARENT, status=Status.COMPLETED)
    on_branch(claims.root / "api", "tm/P", "feature.py", "x = 1\n")
    landing = attach_landing(claims)
    result = claims.start("P", "merger", "s1")
    assert result.action == Action.MERGE and result.job is not None, result.reason

    assert landing.run(result.job) == JobState.SUCCEEDED

    assert marker.exists()


def test_a_local_top_is_seeded_from_the_remote_branch_it_already_has(tmp_path: Path) -> None:
    claims = spec_estate(tmp_path)
    api = claims.root / "api"
    seeded = seed_origin_release(api)
    on_branch(api, "tm/T1", "feature.py", "x = 1\n", base=f"origin/{RELEASE}")

    job, state = land_t1(claims)

    assert state == JobState.SUCCEEDED
    assert job.result["base_sha"] == seeded
    assert git(api, "rev-parse", f"origin/{RELEASE}") == seeded


def test_a_missing_top_is_cut_from_the_fetched_default_branch(tmp_path: Path) -> None:
    claims = spec_estate(tmp_path)
    api = claims.root / "api"
    on_branch(api, "tm/T1", "feature.py", "x = 1\n")
    newer = push_main(api, "newer.txt", "y\n")

    assert land_t1(claims)[1] == JobState.SUCCEEDED

    git(api, "merge-base", "--is-ancestor", newer, RELEASE)


def test_a_local_top_landing_refuses_a_dirty_checked_out_target(tmp_path: Path) -> None:
    claims = spec_estate(tmp_path)
    api = claims.root / "api"
    seed_origin_release(api)
    git(api, "branch", "-q", "--no-track", RELEASE, f"origin/{RELEASE}")
    on_branch(api, "tm/T1", "feature.py", "x = 1\n", base=RELEASE)
    git(api, "checkout", "-q", RELEASE)
    (api / "README.md").write_text("dirty\n")
    before = git(api, "rev-parse", f"refs/heads/{RELEASE}")

    job, state = land_t1(claims)

    assert state == JobState.NEEDS_AGENT
    assert job.result["reason"] == "push_failed"
    assert git(api, "rev-parse", f"refs/heads/{RELEASE}") == before


def test_push_true_pushes_as_before(tmp_path: Path) -> None:
    claims = spec_estate(tmp_path, push=True)
    api = claims.root / "api"
    on_branch(api, "tm/T1", "feature.py", "x = 1\n")

    assert land_t1(claims)[1] == JobState.SUCCEEDED

    git(api, "fetch", "-q", "origin")
    landed = git(api, "ls-remote", "origin", f"refs/heads/{RELEASE}").split()[0]
    assert landed == git(api, "rev-parse", f"origin/{RELEASE}")
    assert git(api, "show", f"origin/{RELEASE}:feature.py") == "x = 1"
