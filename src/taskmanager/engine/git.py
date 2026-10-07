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
    ) -> Path:
        """`--no-track`: a branch cut from `origin/main` would otherwise push to `main`.

        Returns the worktree actually in use, which is `worktree_path` on a fresh checkout but
        the branch's existing worktree when one is already checked out elsewhere: `git worktree
        add` on a branch checked out elsewhere exits 128, and a later stage reclaiming the same
        `tm/<id>` branch at a different `--worktree-dir` is handed its worktree back as it was.
        """
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
            return target

        existing = self.find_worktree(branch_name)
        if existing is not None:
            return existing

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
        return target

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


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=False
    )


def _run(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


def rev_parse(repo: Path, ref: str) -> str:
    """The commit `ref` names in `repo`, or "" when it names none (or `repo` is no repository)."""
    res = _git(repo, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
    return res.stdout.strip() if res.returncode == 0 else ""


def is_ancestor(repo: Path, a: str, b: str) -> bool:
    return _git(repo, "merge-base", "--is-ancestor", a, b).returncode == 0


def diff_quiet(repo: Path, base: str, branch: str) -> bool:
    """True when `branch` changes no file against its merge base with `base`.

    Trees, not commits: a sync merge commit that brought nothing of the branch's own is not a
    change. A git error reads as a change, so nothing completes on a failed comparison.
    """
    return _git(repo, "diff", "--quiet", f"{base}...{branch}").returncode == 0


def fetch(repo: Path) -> bool:
    return _git(repo, "fetch", "-q", "origin", "main").returncode == 0


def ensure_branch(repo: Path, branch: str, base: str) -> bool:
    """Creates `branch` at `base` unless it exists; True when it was created.

    One `update-ref` with an empty old value, so of two claims creating the same branch at once
    exactly one creates it and the other reads False. A bare ref carries no upstream, so a bare
    `git push` from a branch cut from `origin/main` never targets the deploying `main`.
    """
    ref = f"refs/heads/{branch}"
    sha = _run(repo, "rev-parse", "--verify", "--quiet", f"{base}^{{commit}}")
    res = _git(repo, "update-ref", ref, sha, "")
    if res.returncode == 0:
        return True
    if rev_parse(repo, ref):
        return False
    raise subprocess.CalledProcessError(res.returncode, res.args, res.stdout, res.stderr)


def rename_branch(repo: Path, old: str, new: str) -> None:
    _run(repo, "branch", "-m", old, new)


def move_worktree(repo: Path, old: Path, new: Path) -> None:
    _run(repo, "worktree", "move", str(old), str(new))


def merge_no_ff(worktree: Path, ref: str, subject: str) -> bool:
    """False on a conflict, leaving the merge in progress for an agent to resolve."""
    return _git(worktree, "merge", "--no-ff", "--no-edit", "-m", subject, ref).returncode == 0


def update_ref_cas(repo: Path, ref: str, new: str, old: str) -> bool:
    """Moves `ref` to `new` only if it still points at `old`."""
    return _git(repo, "update-ref", ref, new, old).returncode == 0


def ls_remote(repo: Path, ref: str) -> str:
    """The sha `origin` holds for `ref`, or "" when it cannot be read."""
    res = _git(repo, "ls-remote", "origin", ref)
    fields = res.stdout.split()
    return fields[0] if res.returncode == 0 and fields else ""


def push(worktree: Path, target: str) -> bool:
    """Never forced: a refused push means the target moved, and the caller merges it in."""
    return _git(worktree, "push", "-q", "origin", f"HEAD:refs/heads/{target}").returncode == 0


def add_detached_worktree(repo: Path, path: Path, commit: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _run(repo, "worktree", "add", "--detach", str(path), commit)


def settled(worktree: Path, ref: str) -> bool:
    """A handed-over merge is resolved: none in progress, nothing uncommitted, `ref` merged."""
    return (
        not rev_parse(worktree, "MERGE_HEAD")
        and _git(worktree, "status", "--porcelain").stdout.strip() == ""
        and is_ancestor(worktree, ref, "HEAD")
    )
