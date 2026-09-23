import subprocess
from pathlib import Path

import pytest

from taskmanager.core.enums import NodeKind, NodeStatus, RelationType, VerificationType
from taskmanager.core.models import Node, NodeRelation, NodeVerification
from taskmanager.db.connection import DatabaseManager
from taskmanager.db.node_repo import NodeRepository
from taskmanager.db.runtime_repo import RuntimeRepository
from taskmanager.engine.git import GitManager
from taskmanager.engine.graph import GraphEngine
from taskmanager.engine.runtime import ExecutionCoordinator


@pytest.fixture
def test_git_repo(tmp_path: Path) -> Path:
    repo_dir = tmp_path / "git_repo"
    repo_dir.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "ci@example.com"],
        cwd=repo_dir,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "CI User"], cwd=repo_dir, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "commit", "--allow-empty", "-m", "init"],
        cwd=repo_dir,
        check=True,
        capture_output=True,
    )
    return repo_dir


@pytest.fixture
def coordinator_setup(
    tmp_path: Path,
) -> tuple[NodeRepository, RuntimeRepository, GraphEngine, ExecutionCoordinator]:
    db = DatabaseManager(tmp_path / "db")
    db.init_all()
    node_repo = NodeRepository(db)
    runtime_repo = RuntimeRepository(db)
    graph = GraphEngine(node_repo=node_repo, runtime_repo=runtime_repo)
    coordinator = ExecutionCoordinator(
        node_repo=node_repo,
        runtime_repo=runtime_repo,
        graph_engine=graph,
    )
    return node_repo, runtime_repo, graph, coordinator


def test_git_manager_get_common_dir(test_git_repo: Path) -> None:
    git_mgr = GitManager(test_git_repo)
    common_dir = git_mgr.get_common_dir()
    assert common_dir == (test_git_repo / ".git").resolve()


def test_git_manager_create_and_remove_worktree(test_git_repo: Path, tmp_path: Path) -> None:
    git_mgr = GitManager(test_git_repo)
    wt_path = tmp_path / "worktrees" / "TASK-01"

    git_mgr.create_worktree("tm/TASK-01", wt_path)
    assert wt_path.exists()
    assert (wt_path / ".git").exists()

    git_mgr.remove_worktree(wt_path)
    assert not wt_path.exists()


def test_create_worktree_hands_back_a_branch_already_checked_out_elsewhere(
    test_git_repo: Path, tmp_path: Path
) -> None:
    """A fix round claims the same `tm/<id>` branch a review round already cut a worktree
    for, at a different `--worktree-dir`; `git worktree add` on a checked-out branch exits
    128, so the existing worktree is handed back instead."""
    git_mgr = GitManager(test_git_repo)
    first_path = tmp_path / "round-1" / "TASK-01"
    git_mgr.create_worktree("tm/TASK-01", first_path)

    second_path = tmp_path / "round-2" / "TASK-01"
    returned = git_mgr.create_worktree("tm/TASK-01", second_path)

    assert returned == first_path
    assert not second_path.exists()


def test_start_ready_task_acquires_lease_and_locks(
    coordinator_setup: tuple[NodeRepository, RuntimeRepository, GraphEngine, ExecutionCoordinator],
) -> None:
    node_repo, runtime_repo, _, coordinator = coordinator_setup

    node = Node(
        id="AUTH-T01",
        kind=NodeKind.TASK,
        title="Implement Auth",
        status=NodeStatus.NOT_STARTED,
    )
    node_repo.save_node(node)

    v1 = NodeVerification(
        node_id="AUTH-T01",
        verification_type=VerificationType.FILE_EXISTS,
        target_path="src/auth/jwt.py",
    )
    v2 = NodeVerification(
        node_id="AUTH-T01",
        verification_type=VerificationType.FILE_EXISTS,
        target_path="src/auth/utils.py",
    )
    node_repo.add_verification(v1)
    node_repo.add_verification(v2)

    lease = coordinator.start_task(
        task_id="AUTH-T01",
        agent_id="agent-01",
        session_id="sess-01",
        account_id="acc-prod",
    )

    assert lease.task_id == "AUTH-T01"
    assert lease.agent_id == "agent-01"
    assert lease.session_id == "sess-01"
    assert lease.account_id == "acc-prod"
    assert lease.branch_name == "tm/AUTH-T01"
    assert lease.worktree_path is None

    updated_node = node_repo.get_node("AUTH-T01")
    assert updated_node is not None
    assert updated_node.status == NodeStatus.IMPLEMENTING

    assert runtime_repo.is_file_locked("src/auth/jwt.py") is True
    assert runtime_repo.is_file_locked("src/auth/utils.py") is True
    assert runtime_repo.get_lease("AUTH-T01") is not None


def test_collision_rejection_with_active_lease(
    coordinator_setup: tuple[NodeRepository, RuntimeRepository, GraphEngine, ExecutionCoordinator],
) -> None:
    node_repo, _, _, coordinator = coordinator_setup

    t1 = Node(id="TASK-A", kind=NodeKind.TASK, title="Task A", status=NodeStatus.NOT_STARTED)
    t2 = Node(id="TASK-B", kind=NodeKind.TASK, title="Task B", status=NodeStatus.NOT_STARTED)
    node_repo.save_node(t1)
    node_repo.save_node(t2)

    shared_file = "src/shared/module.py"
    node_repo.add_verification(
        NodeVerification(
            node_id="TASK-A",
            verification_type=VerificationType.FILE_EXISTS,
            target_path=shared_file,
        )
    )
    node_repo.add_verification(
        NodeVerification(
            node_id="TASK-B",
            verification_type=VerificationType.FILE_EXISTS,
            target_path=shared_file,
        )
    )

    coordinator.start_task(task_id="TASK-A", agent_id="agent-1", session_id="sess-1")

    # The graph itself now reads TASK-B as BLOCKED_BY_LEASE (not READY) while TASK-A holds the
    # file, so start_task's claims-dict lookup refuses it before reaching its own belt-and-
    # suspenders collision check below -- that check still exists for the genuine TOCTOU race
    # (another claim landing between the read and the write), covered separately.
    with pytest.raises(
        ValueError, match=r"Task TASK-B is not ready to start \(current state: BLOCKED_BY_LEASE\)"
    ):
        coordinator.start_task(task_id="TASK-B", agent_id="agent-2", session_id="sess-2")

    unchanged = node_repo.get_node("TASK-B")
    assert unchanged is not None
    assert unchanged.status == NodeStatus.NOT_STARTED


def test_start_not_ready_task_raises_value_error(
    coordinator_setup: tuple[NodeRepository, RuntimeRepository, GraphEngine, ExecutionCoordinator],
) -> None:
    node_repo, _, _, coordinator = coordinator_setup

    t1 = Node(id="AUTH-01", kind=NodeKind.TASK, title="Dep Task", status=NodeStatus.NOT_STARTED)
    t2 = Node(id="AUTH-02", kind=NodeKind.TASK, title="Blocked Task", status=NodeStatus.NOT_STARTED)
    node_repo.save_node(t1)
    node_repo.save_node(t2)

    node_repo.add_relation(
        NodeRelation(
            source_id="AUTH-02",
            target_id="AUTH-01",
            relation_type=RelationType.DEPENDS_ON,
        )
    )

    with pytest.raises(
        ValueError, match="Task AUTH-02 is not ready to start \\(current state: BLOCKED\\)"
    ):
        coordinator.start_task(task_id="AUTH-02", agent_id="agent-1", session_id="sess-1")


def test_heartbeat_and_stop_task(
    coordinator_setup: tuple[NodeRepository, RuntimeRepository, GraphEngine, ExecutionCoordinator],
) -> None:
    node_repo, runtime_repo, _, coordinator = coordinator_setup

    node = Node(id="T-HB", kind=NodeKind.TASK, title="HB Task", status=NodeStatus.NOT_STARTED)
    node_repo.save_node(node)
    node_repo.add_verification(
        NodeVerification(
            node_id="T-HB",
            verification_type=VerificationType.FILE_EXISTS,
            target_path="hb.py",
        )
    )

    coordinator.start_task(task_id="T-HB", agent_id="agent-1", session_id="sess-1")

    assert coordinator.heartbeat("T-HB") is True
    assert coordinator.heartbeat("UNKNOWN") is False

    coordinator.stop_task(task_id="T-HB", new_status=NodeStatus.WAITING_REVIEW)

    assert runtime_repo.get_lease("T-HB") is None
    assert runtime_repo.is_file_locked("hb.py") is False

    updated = node_repo.get_node("T-HB")
    assert updated is not None
    assert updated.status == NodeStatus.WAITING_REVIEW


def test_start_task_and_stop_task_with_worktree(
    test_git_repo: Path,
    tmp_path: Path,
) -> None:
    db = DatabaseManager(tmp_path / "db")
    db.init_all()
    node_repo = NodeRepository(db)
    runtime_repo = RuntimeRepository(db)
    graph = GraphEngine(node_repo=node_repo, runtime_repo=runtime_repo)
    git_mgr = GitManager(test_git_repo)
    coordinator = ExecutionCoordinator(
        node_repo=node_repo,
        runtime_repo=runtime_repo,
        graph_engine=graph,
        git_mgr=git_mgr,
    )

    node = Node(id="WT-01", kind=NodeKind.TASK, title="WT Task", status=NodeStatus.NOT_STARTED)
    node_repo.save_node(node)

    worktree_base = tmp_path / "worktrees"
    lease = coordinator.start_task(
        task_id="WT-01",
        agent_id="agent-1",
        session_id="sess-1",
        create_worktree=True,
        worktree_base=worktree_base,
    )

    expected_wt = worktree_base / "WT-01"
    assert lease.worktree_path == str(expected_wt)
    assert expected_wt.exists()
    assert (expected_wt / ".git").exists()

    coordinator.stop_task(
        task_id="WT-01",
        new_status=NodeStatus.COMPLETED,
        remove_worktree=True,
    )

    assert not expected_wt.exists()
    assert runtime_repo.get_lease("WT-01") is None
    updated = node_repo.get_node("WT-01")
    assert updated is not None
    assert updated.status == NodeStatus.COMPLETED


def test_start_task_reclaims_the_existing_worktree_at_a_different_worktree_dir(
    test_git_repo: Path,
    tmp_path: Path,
) -> None:
    """WAITING_FIXES re-claimed with `--worktree-dir` pointed somewhere new: the review round's
    worktree for `tm/<id>` is still checked out, so it is handed back as it was rather than
    failing on `git worktree add`'s exit 128 for a branch checked out elsewhere."""
    db = DatabaseManager(tmp_path / "db")
    db.init_all()
    node_repo = NodeRepository(db)
    runtime_repo = RuntimeRepository(db)
    graph = GraphEngine(node_repo=node_repo, runtime_repo=runtime_repo)
    git_mgr = GitManager(test_git_repo)
    coordinator = ExecutionCoordinator(
        node_repo=node_repo,
        runtime_repo=runtime_repo,
        graph_engine=graph,
        git_mgr=git_mgr,
    )

    node = Node(id="WT-02", kind=NodeKind.TASK, title="WT Task", status=NodeStatus.WAITING_FIXES)
    node_repo.save_node(node)

    first_base = tmp_path / "round-1"
    first_lease = coordinator.start_task(
        task_id="WT-02",
        agent_id="agent-1",
        session_id="sess-1",
        create_worktree=True,
        worktree_base=first_base,
    )
    coordinator.stop_task(task_id="WT-02", new_status=NodeStatus.WAITING_FIXES)
    node = node_repo.get_node("WT-02")
    assert node is not None
    node.status = NodeStatus.WAITING_FIXES
    node_repo.save_node(node)

    second_base = tmp_path / "round-2"
    second_lease = coordinator.start_task(
        task_id="WT-02",
        agent_id="agent-2",
        session_id="sess-2",
        create_worktree=True,
        worktree_base=second_base,
    )

    assert second_lease.worktree_path == first_lease.worktree_path
    assert not (second_base / "WT-02").exists()
