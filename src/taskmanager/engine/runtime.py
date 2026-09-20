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
        ttl_seconds: int | None = None,
    ) -> Lease:
        state = self.graph.resolve_task_state(task_id)
        # One claim per stage of the lifecycle: the implementer, the reviewer and the fixer each
        # hold a lease, so what is in flight is always `tm run list`.
        claims = {
            VirtualStatus.READY: NodeStatus.IMPLEMENTING,
            NodeStatus.WAITING_REVIEW: NodeStatus.REVIEWING,
            NodeStatus.WAITING_FIXES: NodeStatus.FIXING,
        }
        if state not in claims:
            raise ValueError(f"Task {task_id} is not ready to start (current state: {state})")
        claimed_status = claims[state]
        if claimed_status == NodeStatus.REVIEWING and create_worktree:
            raise ValueError("a review reads the branch; it does not cut a worktree")

        # A review reads and writes nothing, so it locks no file.
        declared_files = (
            [] if claimed_status == NodeStatus.REVIEWING else self.node_repo.declared_files(task_id)
        )

        conflicts = self.runtime_repo.get_conflicting_tasks(declared_files)
        if conflicts:
            raise ValueError(f"Cannot claim task {task_id} due to file collision: {conflicts}")

        worktree_path_str: str | None = None
        branch_name = f"tm/{task_id}"
        if create_worktree:
            if self.git is None or worktree_base is None:
                raise ValueError("a worktree needs a git repository and a worktree directory")
            node = self.node_repo.get_node(task_id)
            repo_name = node.target_repo if node else None
            repo_dir = self.git.root / repo_name if repo_name else self.git.root
            if not (repo_dir / ".git").exists():
                raise ValueError(f"{repo_dir} is not a git repository: set the task's target_repo")
            repo_git = GitManager(repo_dir)
            worktree_dir = Path(worktree_base) / (
                f"{repo_name}-{task_id}" if repo_name else task_id
            )
            repo_git.create_worktree(
                branch_name=branch_name,
                worktree_path=worktree_dir,
                base_ref=repo_git.default_base_ref(),
            )
            worktree_path_str = str(worktree_dir)

        lease = Lease(
            task_id=task_id,
            agent_id=agent_id,
            session_id=session_id,
            account_id=account_id,
            worktree_path=worktree_path_str,
            branch_name=branch_name,
            ttl_seconds=ttl_seconds or 300,
        )
        locks = [FileLock(file_path=p, task_id=task_id) for p in declared_files]
        self.runtime_repo.acquire_lease(lease, locks)

        node = self.node_repo.get_node(task_id)
        if node is None:
            raise ValueError(f"Task '{task_id}' not found")
        node.status = claimed_status
        node.updated_at = datetime.now(tz=UTC)
        self.node_repo.save_node(node)

        return lease

    def _worktree_of(self, task_id: str) -> str | None:
        assert self.git is not None
        node = self.node_repo.get_node(task_id)
        repo_dir = self.git.root / node.target_repo if node and node.target_repo else self.git.root
        if not (repo_dir / ".git").exists():
            return None
        found = GitManager(repo_dir).find_worktree(f"tm/{task_id}")
        return str(found) if found else None

    def heartbeat(self, task_id: str) -> bool:
        return self.runtime_repo.heartbeat(task_id)

    def stop_task(
        self,
        task_id: str,
        new_status: NodeStatus = NodeStatus.WAITING_REVIEW,
        remove_worktree: bool = False,
    ) -> None:
        lease = self.runtime_repo.get_lease(task_id)
        if remove_worktree and self.git:
            # A task waiting for merge holds no lease, so its worktree is found by its branch.
            recorded = lease.worktree_path if lease and lease.worktree_path else None
            found = recorded or self._worktree_of(task_id)
            if found:
                self.git.remove_worktree(Path(found))

        self.runtime_repo.release_lease(task_id)

        node = self.node_repo.get_node(task_id)
        if node is None:
            raise ValueError(f"Task '{task_id}' not found")
        node.status = new_status
        node.updated_at = datetime.now(tz=UTC)
        self.node_repo.save_node(node)
