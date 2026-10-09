import shutil
import subprocess
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from taskmanager.cli.main import app
from taskmanager.engine.doctor import INSTALL

runner = CliRunner()
# Resolved before any test narrows PATH to its fake tools.
GIT = shutil.which("git") or "git"


@pytest.fixture
def project(tmp_path: Path) -> Path:
    path = (tmp_path / "project").resolve()
    res = subprocess.run([GIT, "init", "-q", str(path)], capture_output=True, check=False)
    assert res.returncode == 0, res.stderr
    return path


@pytest.fixture
def bin_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "bin"
    path.mkdir()
    monkeypatch.setenv("PATH", str(path))
    return path


def _tool(bin_dir: Path, name: str, version: str) -> None:
    path = bin_dir / name
    path.write_text(f'#!/bin/sh\necho "{version}"\n', encoding="utf-8")
    path.chmod(0o755)


def test_doctor_prints_each_fact_and_exits_zero_without_codegraph(
    project: Path, bin_dir: Path
) -> None:
    _tool(bin_dir, "git", "git version 2.55.0")

    res = runner.invoke(app, ["doctor", "-C", str(project)])

    assert res.exit_code == 0, res.stdout
    lines = res.stdout.splitlines()
    assert lines[0] == "git: 2.55.0"
    assert lines[1].startswith("python: 3.")
    assert lines[2:] == [
        f"plugin: missing (recommended) -> {INSTALL}",
        "codegraph: missing (recommended) -> npm install -g @colbymchenry/codegraph",
        f"codegraph index (.): missing (recommended) -> codegraph init {project}",
    ]


def test_doctor_exits_one_when_git_is_missing(project: Path, bin_dir: Path) -> None:
    _tool(bin_dir, "codegraph", "1.6.0")

    res = runner.invoke(app, ["doctor", "-C", str(project)])

    assert res.exit_code == 1
    assert "git: missing (required) -> https://git-scm.com/downloads" in res.stdout.splitlines()


def test_doctor_without_git_or_a_root_flag_still_reports(
    project: Path, bin_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("TM_ROOT")
    monkeypatch.chdir(project)

    res = runner.invoke(app, ["doctor"])

    assert res.exit_code == 1, res.stdout
    assert res.stdout.splitlines()[0] == "git: missing (required) -> https://git-scm.com/downloads"


def test_doctor_yaml_prints_the_same_facts(project: Path, bin_dir: Path) -> None:
    _tool(bin_dir, "git", "git version 2.55.0")
    _tool(bin_dir, "codegraph", "1.6.0")
    (project / ".codegraph").mkdir()
    (project / ".codegraph" / "codegraph.db").touch()

    res = runner.invoke(app, ["doctor", "--yaml", "-C", str(project)])

    assert res.exit_code == 0, res.stdout
    facts = {f["name"]: f for f in yaml.safe_load(res.stdout)}
    assert list(facts) == ["git", "python", "plugin", "codegraph", "codegraph index (.)"]
    assert facts["codegraph"] == {
        "name": "codegraph",
        "required": False,
        "ok": True,
        "found": "1.6.0",
        "fix": None,
    }
    assert facts["codegraph index (.)"]["found"] == str(project / ".codegraph")


def test_init_ends_with_the_doctor_summary(project: Path, bin_dir: Path) -> None:
    _tool(bin_dir, "git", "git version 2.55.0")
    _tool(bin_dir, "codegraph", "1.6.0")

    res = runner.invoke(app, ["init", "-C", str(project)])

    assert res.exit_code == 0, res.stdout
    lines = res.stdout.splitlines()
    assert lines[0] == f"Initialized .taskmanager in {project}"
    assert lines[1] == "git: 2.55.0"
    assert lines[3:] == [
        f"plugin: missing (recommended) -> {INSTALL}",
        "codegraph: 1.6.0",
        f"codegraph index (.): missing (recommended) -> codegraph init {project}",
        (
            "next: read `tm guide overview`, then give every repository a task lands in a main "
            'gate: tm config set repos.<repo>.gates.main.command "<command>"'
        ),
    ]


def test_doctor_with_the_plugin_at_another_version_prints_the_install_sh_fix(
    project: Path, bin_dir: Path
) -> None:
    _tool(bin_dir, "git", "git version 2.55.0")
    claude = bin_dir / "claude"
    listed = '[{"id": "taskmanager@taskmanager", "version": "0.0.1"}]'
    claude.write_text(f"#!/bin/sh\necho '{listed}'\n", encoding="utf-8")
    claude.chmod(0o755)

    res = runner.invoke(app, ["doctor", "-C", str(project)])

    assert res.exit_code == 1, res.stdout
    plugin = next(line for line in res.stdout.splitlines() if line.startswith("plugin: "))
    assert plugin.startswith("plugin: 0.0.1 (required) -> tm is ")
    assert plugin.endswith(f"; reinstall both: {INSTALL}")
    assert "install.sh" in INSTALL
