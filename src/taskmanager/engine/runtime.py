from datetime import UTC, datetime
from pathlib import Path

from taskmanager.core.enums import NodeStatus, VirtualStatus
from taskmanager.core.models import FileLock, Lease
from taskmanager.db.node_repo import NodeRepository
from taskmanager.db.runtime_repo import RuntimeRepository
from taskmanager.engine.git import GitManager
from taskmanager.engine.graph import GraphEngine


class ExecutionCoordinator:
    def __init__(
        self,
        node_repo: NodeRepository,
        runtime_repo: RuntimeRepository,
        graph_engine: GraphEngine,
        git_mgr: GitManager | None = None,
    ) -> None:
        self.node_repo = node_repo
        self.runtime_repo = runtime_repo
        self.graph = graph_engine
        self.graph_engine = graph_engine
        self.git = git_mgr
        self.git_mgr = git_mgr

    def start_task(
        self,
        task_id: str,
        agent_id: str,
        session_id: str,
        account_id: str | None = None,
        create_worktree: bool = False,
        worktree_base: Path | None = None,
    ) -> Lease:
        state = self.graph.resolve_task_state(task_id)
        if state != VirtualStatus.READY:
            raise ValueError(f"Task {task_id} is not ready to start (current state: {state})")

        verifications = self.node_repo.get_verifications(task_id)
        declared_files = list(dict.fromkeys(v.target_path for v in verifications if v.target_path))

        conflicts = self.runtime_repo.get_conflicting_tasks(declared_files)
        if conflicts:
            raise ValueError(f"Cannot claim task {task_id} due to file collision: {conflicts}")

        worktree_path_str: str | None = None
        branch_name = f"tm/{task_id}"
        if create_worktree and self.git and worktree_base:
            worktree_dir = Path(worktree_base) / task_id
            self.git.create_worktree(branch_name=branch_name, worktree_path=worktree_dir)
            worktree_path_str = str(worktree_dir)

        lease = Lease(
            task_id=task_id,
            agent_id=agent_id,
            session_id=session_id,
            account_id=account_id,
            worktree_path=worktree_path_str,
            branch_name=branch_name,
        )
        locks = [FileLock(file_path=p, task_id=task_id) for p in declared_files]
        self.runtime_repo.acquire_lease(lease, locks)

        node = self.node_repo.get_node(task_id)
        if node is None:
            raise ValueError(f"Task '{task_id}' not found")
        node.status = NodeStatus.IMPLEMENTING
        node.updated_at = datetime.now(tz=UTC)
        self.node_repo.save_node(node)

        return lease

    def heartbeat(self, task_id: str) -> bool:
        return self.runtime_repo.heartbeat(task_id)

    def stop_task(
        self,
        task_id: str,
        new_status: NodeStatus = NodeStatus.WAITING_REVIEW,
        remove_worktree: bool = False,
    ) -> None:
        lease = self.runtime_repo.get_lease(task_id)
        if remove_worktree and lease and lease.worktree_path and self.git:
            self.git.remove_worktree(Path(lease.worktree_path))

        self.runtime_repo.release_lease(task_id)

        node = self.node_repo.get_node(task_id)
        if node is None:
            raise ValueError(f"Task '{task_id}' not found")
        node.status = new_status
        node.updated_at = datetime.now(tz=UTC)
        self.node_repo.save_node(node)
