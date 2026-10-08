"""Every write that leaves a node COMPLETED removes the worktree its branch is checked out in."""

import logging
from collections.abc import Callable
from pathlib import Path

import pytest
from lifecycle_estate import add, attach_landing, commit, git, make_estate, on_branch, stored

from taskmanager.core.enums import CONTAINERS, NodeKind
from taskmanager.core.status import Action, JobState, Status
from taskmanager.engine.claims import Claims
from taskmanager.engine.config import Gate, ProjectConfig, RepoConfig
from taskmanager.renderers.importers import BulkImporter

# The node that reaches COMPLETED, its worktree, and the write that completes it.
Completion = tuple[str, Path, Callable[[], object]]


def estate(tmp_path: Path) -> Claims:
    gate = Gate(command="true", timeout=60)
    return make_estate(
        tmp_path, config=ProjectConfig(repos={"api": RepoConfig(gates={"main": gate})})
    )


def checked_out(claims: Claims, node_id: str) -> Path:
    """`tm/<node_id>` cut from origin/main into the worktree a claim would give it."""
    container = stored(claims, node_id).kind in CONTAINERS
    path = claims.root / ".worktrees" / (f"{node_id}/api" if container else f"api-{node_id}")
    branch = f"tm/{node_id}"
    git(
        claims.root / "api",
        "worktree",
        "add",
        "-q",
        "--no-track",
        "-b",
        branch,
        str(path),
        "origin/main",
    )
    return path


def import_completed(claims: Claims, node_id: str, repo: str = "api") -> None:
    task = {"id": node_id, "title": node_id, "status": "COMPLETED", "target_repo": repo}
    BulkImporter(claims.nodes, claims.ops).import_dict(
        {"tasks": [{**task, "review": False, "fix": False}]}
    )


def landing(claims: Claims) -> Completion:
    add(claims, "T1", review=False)
    claimed = claims.start("T1", "builder", "s1")
    assert claimed.action == Action.IMPLEMENT and claimed.worktree is not None
    worktree = Path(claimed.worktree)
    commit(worktree, "feature.py", "x = 1\n", "feature")
    claims.complete("T1", token=claimed.token)
    engine = attach_landing(claims)

    def land() -> None:
        merge = claims.start("T1", "merger", "s1")
        assert merge.action == Action.MERGE and merge.job is not None, merge.reason
        assert engine.run(merge.job) == JobState.SUCCEEDED

    return "T1", worktree, land


def importing(claims: Claims) -> Completion:
    add(claims, "T1", review=False)
    return "T1", checked_out(claims, "T1"), lambda: import_completed(claims, "T1")


def superseding(claims: Claims) -> Completion:
    add(claims, "P1", NodeKind.PLAN)
    add(claims, "T1", parent="P1")
    add(claims, "T2")
    return "P1", checked_out(claims, "P1"), lambda: claims.ops.supersede("T1", "T2")


def answering(claims: Claims) -> Completion:
    add(claims, "P1", NodeKind.PLAN)
    add(claims, "T0", parent="P1", status=Status.COMPLETED)
    add(claims, "T1", parent="P1")
    decision = claims.ops.add_decision(
        "Is T1 still needed?", options=["a|Abandon it|T0 covers it|abandon"], blocks=["T1"]
    )
    return "P1", checked_out(claims, "P1"), lambda: claims.ops.answer_decision(decision, "a")


def rolling_up(claims: Claims) -> Completion:
    add(claims, "P1", NodeKind.PLAN)
    add(claims, "T1", parent="P1", review=False, status=Status.IMPLEMENTED)
    worktree = checked_out(claims, "P1")
    api = claims.root / "api"
    on_branch(api, "tm/T1", "feature.py", "x = 1\n")
    git(api, "push", "-q", "origin", "tm/T1:main")
    return "P1", worktree, lambda: claims.reset("T1", Status.COMPLETED, "landed by hand")


PATHS = [landing, importing, superseding, answering, rolling_up]
IDS = ["landing", "import", "supersede", "decision-answer", "container-rollup"]


@pytest.mark.parametrize("path", PATHS, ids=IDS)
def test_completing_a_node_removes_its_worktree_and_keeps_its_branch(
    tmp_path: Path, path: Callable[[Claims], Completion]
) -> None:
    claims = estate(tmp_path)
    node_id, worktree, complete = path(claims)

    complete()

    assert stored(claims, node_id).status == Status.COMPLETED
    assert not worktree.exists()
    assert str(worktree) not in git(claims.root / "api", "worktree", "list")
    assert git(claims.root / "api", "branch", "--list", f"tm/{node_id}")


@pytest.mark.parametrize("path", PATHS, ids=IDS)
def test_completing_a_node_keeps_a_worktree_holding_uncommitted_work(
    tmp_path: Path, path: Callable[[Claims], Completion]
) -> None:
    claims = estate(tmp_path)
    node_id, worktree, complete = path(claims)
    (worktree / "notes.txt").write_text("keep me\n")

    complete()

    assert stored(claims, node_id).status == Status.COMPLETED
    assert (worktree / "notes.txt").read_text() == "keep me\n"


def test_a_refused_write_to_completed_keeps_the_worktree(tmp_path: Path) -> None:
    claims = estate(tmp_path)
    add(claims, "T1", review=False)
    worktree = checked_out(claims, "T1")
    cycle = {
        "tasks": [
            {"id": "T1", "title": "T1", "status": "COMPLETED", "target_repo": "api"},
            {"id": "T2", "title": "T2", "target_repo": "api", "depends_on": ["T3"]},
            {"id": "T3", "title": "T3", "target_repo": "api", "depends_on": ["T2"]},
        ]
    }

    with pytest.raises(ValueError, match="nothing written: this write makes the step graph cyclic"):
        BulkImporter(claims.nodes, claims.ops).import_dict(cycle)

    assert stored(claims, "T1").status == Status.READY
    assert worktree.exists()


def test_completing_a_node_whose_repository_is_not_cloned_logs_nothing(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    claims = estate(tmp_path)

    with caplog.at_level(logging.WARNING):
        import_completed(claims, "T1", repo="web")

    assert stored(claims, "T1").status == Status.COMPLETED
    assert caplog.records == []
