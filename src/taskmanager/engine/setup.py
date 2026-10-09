import json
import subprocess
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

import yaml

from taskmanager.engine.config import DEFAULT_BRANCH, ConfigError, ConfigStore

# (question, found value) -> the answer; the found value is what an empty answer keeps.
Ask = Callable[[str, str], str]

CI_FILES: Final = (".github/workflows/*.yml", ".github/workflows/*.yaml", ".gitlab-ci.yml")
_ESTATE: Final = frozenset({".taskmanager", ".taskmanager/", "/.taskmanager", "/.taskmanager/"})
IGNORE: Final = "ignore"
TRACK: Final = "track"


@dataclass(frozen=True)
class Flags:
    repos: list[str] = field(default_factory=list)
    gates: dict[str, str] = field(default_factory=dict)
    worktree_dir: str | None = None
    track_estate: bool | None = None


def discover_repos(root: Path) -> list[str]:
    if (root / ".git").exists():
        return ["."]
    return sorted(p.name for p in root.iterdir() if (p / ".git").exists())


def remote_head(repo: Path, remote: str | None) -> str:
    """The branch `remote`'s HEAD names, or with no remote the one checked out in `repo`."""
    ref = f"refs/remotes/{remote}/HEAD" if remote else "HEAD"
    res = subprocess.run(
        ["git", "symbolic-ref", "--quiet", "--short", ref],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    branch = res.stdout.strip().removeprefix(f"{remote}/" if remote else "")
    return branch if res.returncode == 0 and branch else DEFAULT_BRANCH


def _commands(node: Any) -> Iterator[str]:
    """Every `run:` string and `script:` entry, the shapes GitHub Actions and GitLab CI write."""
    if isinstance(node, list):
        for item in node:
            yield from _commands(item)
    elif isinstance(node, dict):
        for key, value in node.items():
            if key in ("run", "script") and isinstance(value, str):
                yield value.strip()
            elif key == "script" and isinstance(value, list):
                yield from (v for v in value if isinstance(v, str))
            else:
                yield from _commands(value)


def ci_commands(repo: Path) -> list[str]:
    found: list[str] = []
    for pattern in CI_FILES:
        for path in sorted(repo.glob(pattern)):
            try:
                found.extend(_commands(yaml.safe_load(path.read_text(encoding="utf-8"))))
            except yaml.YAMLError, OSError, UnicodeDecodeError:
                continue
    return list(dict.fromkeys(found))


def _require_repos(root: Path, repos: list[str]) -> None:
    missing = [r for r in repos if not (root / r / ".git").exists()]
    if missing:
        raise ConfigError(f"not a git repository under {root}: {', '.join(missing)}")


def _repo_order(root: Path, store: ConfigStore, flags: Flags, ask: Ask | None) -> list[str]:
    if flags.repos:
        repos = flags.repos
    elif "repo_order" in store.read() or ask is None:
        return list(store.resolve("repo_order").value)
    else:
        answer = ask("repos", ", ".join(discover_repos(root)))
        repos = [r.strip() for r in answer.split(",") if r.strip()]
        if not repos:
            return []
    _require_repos(root, repos)
    store.set("repo_order", json.dumps(repos))
    return repos


def _has_default_branch(store: ConfigStore, repo: str) -> bool:
    return "default_branch" in store.read().get("repos", {}).get(repo, {})


def _default_branches(root: Path, store: ConfigStore, repos: list[str], ask: Ask) -> None:
    for repo in repos:
        if not _has_default_branch(store, repo):
            found = remote_head(root / repo, store.branches().remote(repo))
            branch = ask(f"default branch ({repo})", found)
            store.set(f"repos.{repo}.default_branch", branch)


def _worktree_dir(root: Path, store: ConfigStore, flags: Flags, ask: Ask | None) -> None:
    if flags.worktree_dir is not None:
        store.set("worktree_dir", flags.worktree_dir)
    elif ask is not None and "worktree_dir" not in store.read():
        store.set("worktree_dir", ask("worktree dir", f"../.worktrees/{root.name}"))


def _gates(root: Path, store: ConfigStore, repos: list[str], flags: Flags, ask: Ask | None) -> None:
    _require_repos(root, list(flags.gates))
    for repo, command in flags.gates.items():
        # Without a prompt the default-branch step never runs, so a gate's repo takes its
        # remote's HEAD.
        if not _has_default_branch(store, repo):
            head = remote_head(root / repo, store.branches().remote(repo))
            store.set(f"repos.{repo}.default_branch", head)
        store.set(f"repos.{repo}.gates.main.command", command)
    if ask is None:
        return
    for repo in repos:
        key = f"repos.{repo}.gates.main.command"
        if repo in flags.gates or store.resolve(key).value is not None:
            continue
        found = ci_commands(root / repo)
        lead = [f"{repo} CI runs:", *found] if found else []
        if command := ask("\n".join([*lead, f"main gate ({repo})"]), "").strip():
            store.set(key, command)


def estate_choice(root: Path) -> str | None:
    """What the root's .gitignore says about the estate: ignored, re-included, or nothing."""
    gitignore = root / ".gitignore"
    lines = gitignore.read_text(encoding="utf-8").splitlines() if gitignore.is_file() else []
    for line in reversed([line.strip() for line in lines]):
        if line in _ESTATE:
            return IGNORE
        if line.startswith("!") and line[1:] in _ESTATE:
            return TRACK
    return None


def _in_work_tree(root: Path) -> bool:
    res = subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    return res.returncode == 0 and res.stdout.strip() == "true"


def _estate(root: Path, flags: Flags, ask: Ask | None) -> None:
    """Ignored puts the estate in .gitignore. Tracked re-includes it there, which outranks the
    .git/info/exclude line `tm init` writes."""
    if flags.track_estate is not None:
        choice = TRACK if flags.track_estate else IGNORE
    elif ask is None or estate_choice(root) is not None or not _in_work_tree(root):
        return
    else:
        choice = ask(f"estate ({IGNORE} or {TRACK})", IGNORE).strip()
        if choice not in (IGNORE, TRACK):
            raise ConfigError(f"estate: '{choice}' is neither {IGNORE} nor {TRACK}")
    gitignore = root / ".gitignore"
    text = gitignore.read_text(encoding="utf-8") if gitignore.is_file() else ""
    kept = [line for line in text.splitlines() if line.strip().removeprefix("!") not in _ESTATE]
    kept.append("/.taskmanager/" if choice == IGNORE else "!/.taskmanager/")
    gitignore.write_text("".join(f"{line}\n" for line in kept), encoding="utf-8")


def configure(root: Path, store: ConfigStore, flags: Flags, ask: Ask | None) -> None:
    """Flags always apply. With `ask`, every step not already set asks; without it, nothing is
    asked and only flags change the configuration."""
    repos = _repo_order(root, store, flags, ask)
    if ask is not None:
        _default_branches(root, store, repos, ask)
    _worktree_dir(root, store, flags, ask)
    _gates(root, store, repos, flags, ask)
    _estate(root, flags, ask)
