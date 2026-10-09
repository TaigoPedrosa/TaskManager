import json
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from taskmanager import __version__
from taskmanager.engine.config import ConfigStore

PYTHON_MIN: Final = (3, 14)
GIT_INSTALL: Final = "https://git-scm.com/downloads"
CODEGRAPH_INSTALL: Final = "npm install -g @colbymchenry/codegraph"
INSTALL: Final = (
    "curl -fsSL https://raw.githubusercontent.com/TaigoPedrosa/TaskManager/main/install.sh | bash"
)
PLUGIN: Final = "taskmanager@taskmanager"


@dataclass(frozen=True)
class Fact:
    name: str
    required: bool
    ok: bool
    found: str | None
    fix: str | None

    def line(self) -> str:
        if self.ok:
            return f"{self.name}: {self.found}"
        need = "required" if self.required else "recommended"
        return f"{self.name}: {self.found or 'missing'} ({need}) -> {self.fix}"


def _version(binary: str) -> str | None:
    """The last word of `<binary> --version`'s first line, its path when it prints none, or
    None when it is not on PATH."""
    path = shutil.which(binary)
    if path is None:
        return None
    try:
        res = subprocess.run(
            [path, "--version"], capture_output=True, text=True, check=False, timeout=10
        )
    except OSError, subprocess.SubprocessError:
        return path
    words = (res.stdout.strip().splitlines() or [""])[0].split()
    return words[-1] if words else path


def _tool(name: str, required: bool, fix: str) -> Fact:
    found = _version(name)
    return Fact(name, required, found is not None, found, None if found is not None else fix)


def _python() -> Fact:
    found = ".".join(str(part) for part in sys.version_info[:3])
    ok = sys.version_info[:2] >= PYTHON_MIN
    fix = f"Python {'.'.join(map(str, PYTHON_MIN))}+, which install.sh fetches: {INSTALL}"
    return Fact("python", True, ok, found, None if ok else fix)


def _plugin_version() -> str | None:
    claude = shutil.which("claude")
    if claude is None:
        return None
    try:
        res = subprocess.run(
            [claude, "plugin", "list", "--json"],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        listed = json.loads(res.stdout)
    except OSError, subprocess.SubprocessError, ValueError:
        return None
    if not isinstance(listed, list):
        return None
    versions = [p.get("version") for p in listed if isinstance(p, dict) and p.get("id") == PLUGIN]
    return next((str(v) for v in versions if v), None)


def _plugin() -> Fact:
    found = _plugin_version()
    if found is None:
        return Fact("plugin", False, False, None, INSTALL)
    if found != __version__:
        # Skills of one version read guides and verbs of another: tm and the plugin move together.
        return Fact("plugin", True, False, found, f"tm is {__version__}; reinstall both: {INSTALL}")
    return Fact("plugin", True, True, found, None)


def _repos(root: Path) -> list[str]:
    """The configured repositories cloned under the root; the root itself when none is
    configured, as a fresh `tm init` in a clone has it."""
    store = ConfigStore(root)
    named = [*store.resolve("repo_order").value, *store.resolve("repos").value] or ["."]
    return [r for r in dict.fromkeys(named) if (root / r / ".git").exists()]


def codegraph_index(repo: Path) -> Path:
    # codegraph counts a project as initialized only once this database exists; a `.codegraph/`
    # holding its committed `.gitignore` alone is not one.
    return repo / ".codegraph" / "codegraph.db"


def _index(root: Path, repo: str) -> Fact:
    index = root / repo / ".codegraph"
    ok = codegraph_index(root / repo).is_file()
    fix = f"codegraph init {root / repo}"
    return Fact(
        f"codegraph index ({repo})", False, ok, str(index) if ok else None, None if ok else fix
    )


def facts(root: Path) -> list[Fact]:
    git = _tool("git", True, GIT_INSTALL)
    # Reading the configured repositories validates their branch names with git.
    indexes = [_index(root, repo) for repo in _repos(root)] if git.ok else []
    return [
        git,
        _python(),
        _plugin(),
        _tool("codegraph", False, CODEGRAPH_INSTALL),
        *indexes,
    ]


def exit_code(found: list[Fact]) -> int:
    return 1 if any(f.required and not f.ok for f in found) else 0
