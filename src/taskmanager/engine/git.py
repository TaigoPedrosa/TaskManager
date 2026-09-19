import subprocess
from pathlib import Path


class GitManager:
    def __init__(self, repo_root: Path) -> None:
        self.root = Path(repo_root).resolve()
        self.repo_root = self.root

    def get_common_dir(self) -> Path:
        res = subprocess.run(
            ["git", "rev-parse", "--git-common-dir"],
            cwd=self.root,
            capture_output=True,
            text=True,
            check=True,
        )
        common_path = Path(res.stdout.strip())
        if not common_path.is_absolute():
            return (self.root / common_path).resolve()
        return common_path.resolve()

    def create_worktree(
        self, branch_name: str, worktree_path: Path, base_ref: str = "HEAD"
    ) -> None:
        target = Path(worktree_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["git", "worktree", "add", "-b", branch_name, str(target), base_ref],
            cwd=self.root,
            capture_output=True,
            text=True,
            check=True,
        )

    def remove_worktree(self, worktree_path: Path, force: bool = False) -> None:
        target = Path(worktree_path)
        cmd = ["git", "worktree", "remove", str(target)]
        if force:
            cmd.insert(3, "--force")
        subprocess.run(
            cmd,
            cwd=self.root,
            capture_output=True,
            text=True,
            check=True,
        )
