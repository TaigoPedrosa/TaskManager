"""A throwaway estate for engine tests: a `.taskmanager` beside git clones of bare origins.

Nothing here touches the network: every `origin` is a bare repository under the test's tmp_path.
"""

import subprocess
import sys
from pathlib import Path

import yaml

from taskmanager.core.enums import NodeKind, RelationType
from taskmanager.core.models import Node, NodeRelation
from taskmanager.core.status import Merge, Status
from taskmanager.db.cache_repo import CacheRepository
from taskmanager.db.connection import DatabaseManager
from taskmanager.engine.claims import Claims
from taskmanager.engine.config import Gate, ProjectConfig
from taskmanager.engine.landing import Landing


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def commit(worktree: Path, path: str, content: str | None, message: str) -> str:
    """Writes `content` to `path` (None deletes it) and commits everything in the worktree."""
    target = worktree / path
    if content is None:
        target.unlink()
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    git(worktree, "add", "-A")
    git(worktree, "commit", "-q", "-m", message)
    return git(worktree, "rev-parse", "HEAD")


def make_repo(root: Path, name: str) -> Path:
    origin = root.parent / "origins" / f"{name}.git"
    origin.parent.mkdir(parents=True, exist_ok=True)
    git(origin.parent, "init", "-q", "--bare", "-b", "main", str(origin))
    clone = root / name
    git(root, "clone", "-q", str(origin), str(clone))
    git(clone, "symbolic-ref", "HEAD", "refs/heads/main")
    for key, value in (
        ("user.email", "tm@example.com"),
        ("user.name", "tm"),
        ("commit.gpgsign", "false"),
        ("tag.gpgsign", "false"),
    ):
        git(clone, "config", key, value)
    commit(clone, "README.md", "readme\n", "init")
    git(clone, "push", "-q", "origin", "HEAD:main")
    git(clone, "fetch", "-q", "origin")
    return clone


def _scratch(repo: Path, label: str) -> Path:
    return repo.parent.parent / "scratch" / f"{repo.name}-{label.replace('/', '-')}"


def on_branch(
    repo: Path, branch: str, path: str, content: str | None, base: str = "origin/main"
) -> str:
    """Commits to `branch`, cutting it from `base` when it does not exist yet, without checking
    it out in the clone."""
    scratch = _scratch(repo, branch)
    exists = (
        subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"],
            capture_output=True,
            check=False,
        ).returncode
        == 0
    )
    if exists:
        git(repo, "worktree", "add", "-q", str(scratch), branch)
    else:
        git(repo, "worktree", "add", "-q", "--no-track", "-b", branch, str(scratch), base)
    sha = commit(scratch, path, content, f"{branch}: {path}")
    git(repo, "worktree", "remove", "--force", str(scratch))
    return sha


def push_main(repo: Path, path: str, content: str | None) -> str:
    """Moves `origin/main` by one commit, as another agent's landing would."""
    git(repo, "fetch", "-q", "origin")
    scratch = _scratch(repo, "main")
    git(repo, "worktree", "add", "-q", "--detach", str(scratch), "origin/main")
    sha = commit(scratch, path, content, f"main: {path}")
    git(scratch, "push", "-q", "origin", "HEAD:main")
    git(repo, "worktree", "remove", "--force", str(scratch))
    git(repo, "fetch", "-q", "origin")
    return sha


def branch_at(repo: Path, branch: str, ref: str = "origin/main") -> None:
    git(repo, "branch", "-q", "--no-track", branch, ref)


def make_estate(
    tmp_path: Path, repos: tuple[str, ...] = ("api",), config: ProjectConfig | None = None
) -> Claims:
    root = tmp_path / "estate"
    root.mkdir()
    for name in repos:
        make_repo(root, name)
    cfg = config or ProjectConfig()
    tm_dir = root / ".taskmanager"
    tm_dir.mkdir()
    (tm_dir / "config.yaml").write_text(
        yaml.safe_dump(cfg.model_dump(mode="json", exclude_defaults=True))
    )
    DatabaseManager(tm_dir).init_all()
    return Claims.open(root, cfg)


def add(
    claims: Claims,
    node_id: str,
    kind: NodeKind = NodeKind.TASK,
    *,
    parent: str | None = None,
    repo: str | None = "api",
    merge: Merge = Merge.MAIN,
    review: bool | None = None,
    fix: bool | None = None,
    status: Status = Status.READY,
    models: list[str] | None = None,
    files: list[str] | None = None,
    depends: tuple[str, ...] = (),
    **columns: object,
) -> None:
    """Saves a node straight through the repository: these tests exercise the claims engine,
    not the write-time validation in front of it."""
    container = kind in (NodeKind.PLAN, NodeKind.SPEC)
    reviewed = (not container) if review is None else review
    claims.nodes.save_node(
        Node(
            id=node_id,
            kind=kind,
            title=node_id,
            status=status,
            target_repo=None if container else repo,
            review=reviewed,
            fix=reviewed if fix is None else fix,
            merge=merge,
            acceptable_models=models or [],
            frontmatter={"declared_files": files} if files else {},
            **columns,
        )
    )
    if parent is not None:
        claims.nodes.add_relation(
            NodeRelation(source_id=parent, target_id=node_id, relation_type=RelationType.CONTAINS)
        )
    for dep in depends:
        claims.nodes.add_relation(
            NodeRelation(source_id=node_id, target_id=dep, relation_type=RelationType.DEPENDS_ON)
        )


def stored(claims: Claims, node_id: str) -> Node:
    found = claims.nodes.get_node(node_id)
    assert found is not None
    return found


def section(claims: Claims, node_id: str, key: str) -> str:
    found = claims.nodes.get_section(node_id, key)
    return found.content if found else ""


GATE_SCRIPT = """
import pathlib, sys
worktree, log = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
listed = worktree / "failing.txt"
names = listed.read_text().split() if listed.exists() else []
cases = "".join(f'<testcase classname="suite" name="{n}"><failure/></testcase>' for n in names)
(worktree / "report.xml").write_text(
    f'<testsuite><testcase classname="suite" name="ok"/>{cases}</testsuite>'
)
with log.open("a") as out:
    out.write(worktree.name + "\\n")
sys.exit(1 if names else 0)
"""


def junit_gate(tmp_path: Path) -> Gate:
    """A gate that fails the tests named in the worktree's failing.txt, reports them as JUnit
    and logs the worktree it ran in (a baseline's is named `<job>-base`)."""
    script = tmp_path / "gate.py"
    script.write_text(GATE_SCRIPT)
    log = tmp_path / "gate.log"
    return Gate(
        command=f"{sys.executable} {script} {{worktree}} {log}", junit="report.xml", timeout=60
    )


def gate_runs(tmp_path: Path) -> list[str]:
    log = tmp_path / "gate.log"
    return log.read_text().split() if log.exists() else []


def attach_landing(claims: Claims, detach: bool = False) -> Landing:
    """detach=False records jobs without spawning, so a test runs each one in-process."""
    return Landing(
        claims.root, claims.config, claims, CacheRepository(claims.nodes.db), claims.jobs, detach
    )
