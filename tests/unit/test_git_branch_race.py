import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest
from lifecycle_estate import branch_at, commit, git, make_repo

from taskmanager.engine import git as gitops


def repository(tmp_path: Path) -> Path:
    root = tmp_path / "estate"
    root.mkdir()
    return make_repo(root, "api")


def test_ensure_branch_called_twice_creates_it_then_reports_it_exists(tmp_path: Path) -> None:
    repo = repository(tmp_path)

    assert gitops.ensure_branch(repo, "tm/P", "origin/main") is True
    assert gitops.ensure_branch(repo, "tm/P", "origin/main") is False
    assert git(repo, "rev-parse", "refs/heads/tm/P") == git(repo, "rev-parse", "origin/main")


def test_ensure_branch_from_two_threads_at_once_creates_it_once_and_neither_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = repository(tmp_path)
    run = subprocess.run
    barrier = threading.Barrier(2, timeout=30)
    called = threading.local()

    # Each thread waits after its first git call until the other has made its own, so a shape
    # that checks before it creates has both checks answered before either creates.
    def in_step(*args: Any, check: bool = False, **kwargs: Any) -> Any:
        result = run(*args, check=check, **kwargs)
        if not getattr(called, "once", False):
            called.once = True
            barrier.wait()
        return result

    with monkeypatch.context() as patch, ThreadPoolExecutor(2) as pool:
        patch.setattr(subprocess, "run", in_step)
        claims = [pool.submit(gitops.ensure_branch, repo, "tm/P", "origin/main") for _ in "ab"]
        created = sorted(claim.result() for claim in claims)

    assert created == [False, True]
    assert git(repo, "rev-parse", "refs/heads/tm/P") == git(repo, "rev-parse", "origin/main")


def test_ensure_branch_on_an_existing_branch_at_another_commit_leaves_it_there(
    tmp_path: Path,
) -> None:
    repo = repository(tmp_path)
    branch_at(repo, "tm/P")
    before = git(repo, "rev-parse", "refs/heads/tm/P")
    ahead = commit(repo, "later.py", "later\n", "later")

    assert gitops.ensure_branch(repo, "tm/P", ahead) is False
    assert git(repo, "rev-parse", "refs/heads/tm/P") == before != ahead


def test_ensure_branch_with_a_name_git_refuses_raises(tmp_path: Path) -> None:
    repo = repository(tmp_path)

    with pytest.raises(subprocess.CalledProcessError):
        gitops.ensure_branch(repo, "tm/bad..name", "origin/main")
