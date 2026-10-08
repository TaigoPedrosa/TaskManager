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
    fix = (
        f"uv tool install --reinstall --python {'.'.join(map(str, PYTHON_MIN))} "
        f"git+https://github.com/TaigoPedrosa/TaskManager@v{__version__}"
    )
    return Fact("python", True, ok, found, None if ok else fix)


def _repos(root: Path) -> list[str]:
    """The configured repositories cloned under the root; the root itself when none is
    configured, as a fresh `tm init` in a clone has it."""
    store = ConfigStore(root)
    named = [*store.resolve("repo_order").value, *store.resolve("repos").value] or ["."]
    return [r for r in dict.fromkeys(named) if (root / r / ".git").exists()]


def _index(root: Path, repo: str) -> Fact:
    index = root / repo / ".codegraph"
    ok = index.is_dir()
    fix = f"codegraph init {root / repo}"
    return Fact(
        f"codegraph index ({repo})", False, ok, str(index) if ok else None, None if ok else fix
    )


def facts(root: Path) -> list[Fact]:
    git = _tool("git", True, GIT_INSTALL)
    # Reading the configured repositories validates their branch names with git.
    indexes = [_index(root, repo) for repo in _repos(root)] if git.ok else []
    return [git, _python(), _tool("codegraph", False, CODEGRAPH_INSTALL), *indexes]


def exit_code(found: list[Fact]) -> int:
    return 1 if any(f.required and not f.ok for f in found) else 0
