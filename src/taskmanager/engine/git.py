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

    def default_base_ref(self) -> str:
        """`origin/main` when the repository has one, else `HEAD`."""
        res = subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", "origin/main"],
            cwd=self.root,
            capture_output=True,
            text=True,
            check=False,
        )
        return "origin/main" if res.returncode == 0 else "HEAD"

    def create_worktree(
        self, branch_name: str, worktree_path: Path, base_ref: str = "HEAD"
    ) -> None:
        """`--no-track`: a branch cut from `origin/main` would otherwise push to `main`."""
        target = Path(worktree_path)
        if target.exists():
            head = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                cwd=target,
                capture_output=True,
                text=True,
                check=False,
            ).stdout.strip()
            if head != branch_name:
                raise ValueError(f"{target} exists and is not the worktree of {branch_name}")
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        branch_exists = (
            subprocess.run(
                ["git", "rev-parse", "--verify", "--quiet", f"refs/heads/{branch_name}"],
                cwd=self.root,
                capture_output=True,
                check=False,
            ).returncode
            == 0
        )
        # A fix round reuses the branch its first round cut.
        cmd = (
            ["git", "worktree", "add", str(target), branch_name]
            if branch_exists
            else ["git", "worktree", "add", "--no-track", "-b", branch_name, str(target), base_ref]
        )
        subprocess.run(cmd, cwd=self.root, capture_output=True, text=True, check=True)

    def find_worktree(self, branch_name: str) -> Path | None:
        """The worktree that has `branch_name` checked out, in this repository, if any."""
        res = subprocess.run(
            ["git", "worktree", "list", "--porcelain"],
            cwd=self.root,
            capture_output=True,
            text=True,
            check=True,
        )
        current: Path | None = None
        for line in res.stdout.splitlines():
            if line.startswith("worktree "):
                current = Path(line[len("worktree ") :])
            elif line == f"branch refs/heads/{branch_name}" and current is not None:
                return current
        return None

    def remove_worktree(self, worktree_path: Path, force: bool = False) -> None:
        """Removed from the repository the worktree was cut from, which is not always `self.root`."""
        target = Path(worktree_path)
        res = subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
            cwd=target,
            capture_output=True,
            text=True,
            check=True,
        )
        owner = Path(res.stdout.strip()).parent
        cmd = ["git", "worktree", "remove", str(target)]
        if force:
            cmd.insert(3, "--force")
        subprocess.run(
            cmd,
            cwd=owner,
            capture_output=True,
            text=True,
            check=True,
        )
