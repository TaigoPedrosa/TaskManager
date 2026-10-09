import subprocess
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner, Result

from taskmanager.cli import main
from taskmanager.cli.main import app

runner = CliRunner()

WORKFLOW = """\
jobs:
  gates:
    steps:
      - uses: actions/checkout@v7
      - run: uv run pytest
      - run: |
          npm ci
          npm test
"""
GITLAB = """\
test:
  script:
    - make check
"""


def git(cwd: Path, *args: str) -> str:
    res = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False)
    assert res.returncode == 0, res.stderr
    return res.stdout


def clone(parent: Path, name: str, branch: str) -> Path:
    """A clone whose origin's HEAD names `branch`."""
    origin = parent.parent / f"{parent.name}-{name}.git"
    git(parent.parent, "init", "-q", "--bare", "-b", branch, str(origin))
    seed = parent.parent / f"{parent.name}-{name}-seed"
    git(parent.parent, "init", "-q", "-b", branch, str(seed))
    git(
        seed,
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@t",
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        "s",
    )
    git(seed, "push", "-q", str(origin), branch)
    git(parent, "clone", "-q", str(origin), name)
    return parent / name


@pytest.fixture
def root(tmp_path: Path) -> Path:
    path = tmp_path / "proj"
    path.mkdir()
    api = clone(path, "api", "trunk")
    (api / ".github" / "workflows").mkdir(parents=True)
    (api / ".github" / "workflows" / "ci.yml").write_text(WORKFLOW, encoding="utf-8")
    (api / ".gitlab-ci.yml").write_text(GITLAB, encoding="utf-8")
    clone(path, "web", "develop")
    return path


@pytest.fixture
def terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(main, "_stdin_is_terminal", lambda: True)


def init(root: Path, *args: str, stdin: str | None = None) -> Result:
    res = runner.invoke(app, ["init", *args, "-C", str(root)], input=stdin)
    assert res.exit_code == 0, res.output
    return res


def config(root: Path) -> dict[str, object]:
    path = root / ".taskmanager" / "config.yaml"
    return yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {}


def gate(root: Path, repo: str) -> object:
    return config(root)["repos"][repo].get("gates", {}).get("main", {}).get("command")


@pytest.mark.usefixtures("terminal")
def test_init_at_a_terminal_walks_each_step_with_found_values(root: Path) -> None:
    res = init(root, stdin="\n\n\n\nmake gate\n\n")

    assert config(root)["repo_order"] == ["api", "web"]
    assert config(root)["repos"]["api"]["default_branch"] == "trunk"
    assert config(root)["repos"]["web"]["default_branch"] == "develop"
    assert config(root)["worktree_dir"] == "../.worktrees/proj"
    assert gate(root, "api") == "make gate"
    assert "web" not in [r for r in config(root)["repos"] if gate(root, r)]
    assert "repos [api, web]:" in res.stdout
    assert (
        "api CI runs:\nuv run pytest\nnpm ci\nnpm test\nmake check\nmain gate (api):" in res.stdout
    )
    assert "web CI runs" not in res.stdout
    assert "main gate (web):" in res.stdout


@pytest.mark.usefixtures("terminal")
def test_init_a_second_time_with_no_input_changes_nothing(root: Path) -> None:
    init(root, stdin="\n\n\n\napi gate\nweb gate\n")
    before = (root / ".taskmanager" / "config.yaml").read_text(encoding="utf-8")

    res = init(root, stdin="")

    assert (root / ".taskmanager" / "config.yaml").read_text(encoding="utf-8") == before
    assert "repos [" not in res.stdout
    assert "main gate (" not in res.stdout


@pytest.mark.usefixtures("terminal")
def test_init_yes_takes_every_found_value_and_asks_nothing(root: Path) -> None:
    res = init(root, "--yes")

    assert not any(
        q in res.stdout for q in ("repos [", "default branch (", "main gate (", "estate (")
    )
    assert config(root)["repo_order"] == ["api", "web"]
    assert config(root)["repos"]["api"]["default_branch"] == "trunk"
    assert config(root)["worktree_dir"] == "../.worktrees/proj"
    assert gate(root, "api") is None


def test_init_on_a_non_terminal_stdin_never_prompts(root: Path) -> None:
    res = init(root)

    assert config(root) == {}
    assert "repos [" not in res.stdout


def test_init_flags_override_the_found_values(root: Path) -> None:
    init(root, "--yes", "--repo", "web", "--worktree-dir", "wt", "--gate", "api=make ci")

    assert config(root)["repo_order"] == ["web"]
    assert config(root)["worktree_dir"] == "wt"
    assert gate(root, "api") == "make ci"


def test_init_flags_apply_without_a_terminal(root: Path) -> None:
    init(root, "--repo", "api", "--gate", "web=npm test")

    assert config(root)["repo_order"] == ["api"]
    assert gate(root, "web") == "npm test"


def test_init_refuses_a_repo_that_is_not_a_git_repository(root: Path) -> None:
    res = runner.invoke(app, ["init", "--repo", "nope", "-C", str(root)])

    assert res.exit_code == 1
    assert "not a git repository" in res.stderr
    assert "Traceback" not in res.output


def test_init_refuses_a_gate_flag_without_a_repo(root: Path) -> None:
    res = runner.invoke(app, ["init", "--gate", "make ci", "-C", str(root)])

    assert res.exit_code == 2
    assert "<repo>=<command>" in res.output


@pytest.fixture
def repo_root(tmp_path: Path) -> Path:
    return clone(tmp_path, "solo", "main")


def ignored(root: Path) -> bool:
    return (
        subprocess.run(
            ["git", "check-ignore", "-q", ".taskmanager/state.db"], cwd=root, check=False
        ).returncode
        == 0
    )


@pytest.mark.usefixtures("terminal")
def test_init_tracks_the_estate_over_the_local_exclude(repo_root: Path) -> None:
    init(repo_root, stdin="\n\n\nmake\ntrack\n")

    assert (repo_root / ".gitignore").read_text(encoding="utf-8") == "!/.taskmanager/\n"
    assert not ignored(repo_root)
    assert "/.taskmanager/" in (repo_root / ".git" / "info" / "exclude").read_text("utf-8")
    res = init(repo_root, stdin="")
    assert "estate (" not in res.stdout


@pytest.mark.usefixtures("terminal")
def test_init_ignores_the_estate_by_default(repo_root: Path) -> None:
    (repo_root / ".gitignore").write_text("dist/\n", encoding="utf-8")

    init(repo_root, stdin="\n\n\n\n\n")

    assert (repo_root / ".gitignore").read_text(encoding="utf-8") == "dist/\n/.taskmanager/\n"


def test_init_estate_flag_replaces_the_earlier_choice(repo_root: Path) -> None:
    (repo_root / ".gitignore").write_text(".taskmanager/\ndist/\n", encoding="utf-8")

    init(repo_root, "--track-estate")
    assert (repo_root / ".gitignore").read_text(encoding="utf-8") == "dist/\n!/.taskmanager/\n"

    init(repo_root, "--ignore-estate")
    assert (repo_root / ".gitignore").read_text(encoding="utf-8") == "dist/\n/.taskmanager/\n"
    assert ignored(repo_root)


@pytest.mark.usefixtures("terminal")
def test_init_skips_the_estate_step_outside_a_git_work_tree(root: Path) -> None:
    res = init(root, stdin="\n\n\n\n\n\n")

    assert "estate (" not in res.stdout
    assert not (root / ".gitignore").exists()


def test_init_refuses_a_gate_for_a_repo_that_is_not_a_git_repository(root: Path) -> None:
    res = runner.invoke(app, ["init", "--gate", "nope=make ci", "-C", str(root)])

    assert res.exit_code == 1
    assert "not a git repository" in res.stderr


@pytest.mark.usefixtures("terminal")
def test_init_refuses_an_estate_answer_that_is_neither_choice(repo_root: Path) -> None:
    res = runner.invoke(app, ["init", "-C", str(repo_root)], input="\n\n\n\nshare\n")

    assert res.exit_code == 1
    assert "neither ignore nor track" in res.stderr
    assert not (repo_root / ".gitignore").exists()


def test_init_yes_without_an_origin_head_proposes_main(tmp_path: Path) -> None:
    git(tmp_path, "init", "-q", "-b", "work", "local")

    init(tmp_path / "local", "--yes")

    assert config(tmp_path / "local")["repos"]["."]["default_branch"] == "main"


def test_init_yes_with_no_repository_writes_no_repo_order(tmp_path: Path) -> None:
    init(tmp_path, "--yes")

    assert "repo_order" not in config(tmp_path)
