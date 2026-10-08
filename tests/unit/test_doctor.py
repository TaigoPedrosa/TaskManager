import re
import shutil
import subprocess
from pathlib import Path

import pytest

from taskmanager.engine import doctor
from taskmanager.engine.config import ConfigStore
from taskmanager.engine.git import valid_branch

# A project's own stack; the runtime tm itself was installed with is no such tool.
STACK_TOOLS = re.compile(r"\b(node|npm|npx|pytest|pip|cargo|go|java|mvn|gradle|jest|vitest)\b")
# Resolved before any test narrows PATH to its fake tools.
GIT = shutil.which("git") or "git"


def _git_init(path: Path) -> Path:
    path.mkdir(parents=True)
    res = subprocess.run([GIT, "init", "-q", str(path)], capture_output=True, check=False)
    assert res.returncode == 0, res.stderr
    return path


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return _git_init(tmp_path / "repo")


@pytest.fixture
def bin_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The only directory on PATH, so a tool is present exactly when a test writes it."""
    path = tmp_path / "bin"
    path.mkdir()
    monkeypatch.setenv("PATH", str(path))
    return path


def _tool(bin_dir: Path, name: str, script: str) -> Path:
    path = bin_dir / name
    path.write_text(f"#!/bin/sh\n{script}\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def _git(bin_dir: Path) -> None:
    version = 'if [ "$1" = --version ]; then echo "git version 2.55.0"; exit 0; fi'
    _tool(bin_dir, "git", f'{version}\nexec "{GIT}" "$@"')


def _codegraph(bin_dir: Path) -> None:
    _tool(bin_dir, "codegraph", "echo 1.6.0")


def _by_name(found: list[doctor.Fact]) -> dict[str, doctor.Fact]:
    return {f.name: f for f in found}


def test_facts_with_codegraph_and_an_index_report_both_found(repo: Path, bin_dir: Path) -> None:
    _git(bin_dir)
    _codegraph(bin_dir)
    (repo / ".codegraph").mkdir()
    (repo / ".codegraph" / "codegraph.db").touch()

    found = doctor.facts(repo)

    facts = _by_name(found)
    assert facts["git"].line() == "git: 2.55.0"
    assert facts["codegraph"].line() == "codegraph: 1.6.0"
    assert facts["codegraph index (.)"].line() == f"codegraph index (.): {repo / '.codegraph'}"
    assert doctor.exit_code(found) == 0


def test_facts_with_codegraph_and_no_index_recommend_codegraph_init(
    repo: Path, bin_dir: Path
) -> None:
    _git(bin_dir)
    _codegraph(bin_dir)

    found = doctor.facts(repo)

    index = _by_name(found)["codegraph index (.)"]
    assert (index.ok, index.required, index.fix) == (False, False, f"codegraph init {repo}")
    assert index.line() == f"codegraph index (.): missing (recommended) -> codegraph init {repo}"
    assert doctor.exit_code(found) == 0


def test_facts_with_a_codegraph_directory_holding_no_database_recommend_codegraph_init(
    repo: Path, bin_dir: Path
) -> None:
    _git(bin_dir)
    _codegraph(bin_dir)
    (repo / ".codegraph").mkdir()
    (repo / ".codegraph" / ".gitignore").write_text("*\n!.gitignore\n", encoding="utf-8")

    index = _by_name(doctor.facts(repo))["codegraph index (.)"]

    assert (index.ok, index.fix) == (False, f"codegraph init {repo}")


def test_facts_without_codegraph_recommend_its_install_and_exit_zero(
    repo: Path, bin_dir: Path
) -> None:
    _git(bin_dir)

    found = doctor.facts(repo)

    assert _by_name(found)["codegraph"].line() == (
        "codegraph: missing (recommended) -> npm install -g @colbymchenry/codegraph"
    )
    assert doctor.exit_code(found) == 0


def test_facts_without_git_exit_non_zero(repo: Path, bin_dir: Path) -> None:
    _codegraph(bin_dir)

    found = doctor.facts(repo)

    assert _by_name(found)["git"].line() == (
        "git: missing (required) -> https://git-scm.com/downloads"
    )
    assert doctor.exit_code(found) == 1


def test_facts_under_too_old_a_python_exit_non_zero(
    repo: Path, bin_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _git(bin_dir)
    monkeypatch.setattr(doctor, "PYTHON_MIN", (99, 0))

    found = doctor.facts(repo)

    python = _by_name(found)["python"]
    assert (python.ok, python.required) == (False, True)
    assert python.fix is not None
    assert "--python 99.0 " in python.fix
    assert doctor.exit_code(found) == 1


def test_facts_require_no_project_stack_tool(
    repo: Path, bin_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(doctor, "PYTHON_MIN", (99, 0))
    _git(bin_dir)

    found = doctor.facts(repo)

    assert [f.name for f in found] == ["git", "python", "codegraph", "codegraph index (.)"]
    required = [f for f in found if f.required]
    assert [f.name for f in required] == ["git", "python"]
    assert [f.fix for f in required if STACK_TOOLS.search(f.fix or "")] == []


def test_facts_check_only_the_configured_repos_that_are_cloned(
    tmp_path: Path, bin_dir: Path
) -> None:
    _git(bin_dir)
    root = tmp_path / "estate"
    _git_init(root / "api")
    _git_init(root / "web")
    store = ConfigStore(root)
    store.set("repo_order", "[web, absent]")
    store.set("repos", "{api: {}, web: {}}")

    names = [f.name for f in doctor.facts(root)]

    assert names[3:] == ["codegraph index (web)", "codegraph index (api)"]


def test_facts_without_git_leave_the_configured_repos_unread(tmp_path: Path, bin_dir: Path) -> None:
    _git(bin_dir)
    root = tmp_path / "estate"
    _git_init(root / "api")
    ConfigStore(root).set("repos", "{api: {default_branch: trunk}}")
    (bin_dir / "git").unlink()
    valid_branch.cache_clear()

    assert [f.name for f in doctor.facts(root)] == ["git", "python", "codegraph"]


def test_facts_check_no_index_where_the_root_is_no_clone(tmp_path: Path, bin_dir: Path) -> None:
    assert [f.name for f in doctor.facts(tmp_path)] == ["git", "python", "codegraph"]


def test_a_tool_printing_no_version_is_found_at_its_path(repo: Path, bin_dir: Path) -> None:
    path = _tool(bin_dir, "codegraph", "true")

    assert _by_name(doctor.facts(repo))["codegraph"].found == str(path)


def test_a_tool_that_cannot_start_is_found_at_its_path(repo: Path, bin_dir: Path) -> None:
    path = bin_dir / "codegraph"
    path.write_text("#!/no/such/interpreter\n", encoding="utf-8")
    path.chmod(0o755)

    assert _by_name(doctor.facts(repo))["codegraph"].found == str(path)


def test_the_taskmanager_skill_runs_tm_doctor_on_first_contact() -> None:
    skill = Path(__file__).resolve().parents[2] / "plugin/skills/taskmanager/SKILL.md"
    row = next(
        line for line in skill.read_text(encoding="utf-8").splitlines() if "first contact" in line
    )
    assert "`tm doctor`" in row
