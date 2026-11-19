import json
import subprocess
from pathlib import Path

import yaml
from click.testing import Result
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from taskmanager.cli.main import app
from taskmanager.web.app import create_app

runner = CliRunner()


def tm(root: Path, *args: str) -> Result:
    return runner.invoke(app, [*args, "-C", str(root)])


def git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def test_the_web_body_shows_the_job_parked_item_inside_the_merge_step_of_a_landing_the_cli_parked(
    tmp_path: Path,
) -> None:
    git(tmp_path, "init", "--bare", "-b", "main", str(tmp_path / "core.git"))
    git(tmp_path, "clone", str(tmp_path / "core.git"), str(tmp_path / "core"))
    core = tmp_path / "core"
    git(core, "config", "user.email", "ci@example.com")
    git(core, "config", "user.name", "CI")
    git(core, "commit", "--allow-empty", "-m", "init")
    git(core, "push", "origin", "HEAD:main")
    git(core, "fetch", "origin")
    for args in (
        ("init",),
        ("spec", "add", "S", "--slug", "S1", "--no-review", "--no-fix"),
        ("plan", "add", "P", "--spec", "S1", "--slug", "P1"),
        ("task", "add", "t", "--plan", "S1-P1", "--slug", "t"),
        ("config", "set", "repos.core.gates.main.command", "true"),
    ):
        assert tm(tmp_path, *args).exit_code == 0
    assert tm(tmp_path, "task", "update", "S1-P1-t", "--repo", "core").exit_code == 0
    git(core, "checkout", "-q", "-b", "tm/S1-P1-t")
    (core / "a.py").write_text("branch\n")
    git(core, "add", "a.py")
    git(core, "commit", "-q", "-m", "branch a")
    git(core, "checkout", "-q", "main")
    (core / "a.py").write_text("main\n")
    git(core, "add", "a.py")
    git(core, "commit", "-q", "-m", "main a")
    git(core, "push", "origin", "main")
    for step in (
        lambda: tm(tmp_path, "task", "complete", "S1-P1-t"),
        lambda: tm(tmp_path, "task", "review", "S1-P1-t", "--approve", "--clean"),
    ):
        res = tm(tmp_path, "task", "start", "S1-P1-t", "--agent", "agent-a", "--session", "s")
        assert res.exit_code == 0, res.output
        done = step()
        assert done.exit_code == 0, done.output
    merge = yaml.safe_load(
        tm(tmp_path, "task", "start", "S1-P1-t", "--agent", "merger", "--session", "s").stdout
    )
    assert merge["action"] == "merge"
    waited = tm(tmp_path, "job", "status", merge["job"], "--wait", "120")
    assert waited.exit_code == 0, waited.output
    assert json.loads(waited.stdout)["state"] == "needs_agent"

    steps = TestClient(create_app(tmp_path)).get("/api/nodes/S1-P1-t").json()["steps"]

    assert [(s["action"], s["ended"]) for s in steps][-1] == ("merge", None)
    assert steps[-1]["items"] == [{"kind": "job parked", "job": merge["job"], "reason": "conflict"}]
