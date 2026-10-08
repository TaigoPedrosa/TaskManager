import json
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest
from lifecycle_estate import add, make_estate
from typer.testing import CliRunner

from taskmanager.cli.main import app
from taskmanager.core.models import Job
from taskmanager.core.status import JobKind, JobState, Status
from taskmanager.db.cache_repo import CacheRepository
from taskmanager.engine import git as gitops
from taskmanager.engine.claims import Claims
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

    def ls_remote(self, _repo: Path, ref: str) -> tuple[str, subprocess.CompletedProcess[str]]:
        head = next(self._heads)
        if head:
            return head, ran("ls-remote", "origin", ref)
        return "", ran("ls-remote", "origin", ref, code=128, stderr=UNREACHABLE)

    def push(self, _worktree: Path, target: str) -> subprocess.CompletedProcess[str]:
        self.pushes.append(target)
        if self.refusals:
            self.refusals -= 1
            return ran("push", "-q", "origin", f"HEAD:refs/heads/{target}", code=1, stderr=REFUSED)
        return ran("push", "-q", "origin", f"HEAD:refs/heads/{target}")


def estate(tmp_path: Path) -> tuple[Claims, Landing, list[float]]:
    claims = make_estate(tmp_path)
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

    monkeypatch.setattr(gitops, "fetch", lambda repo, branch: True)
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
