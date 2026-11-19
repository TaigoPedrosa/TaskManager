import json
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from taskmanager.cli.main import app
from taskmanager.engine import discovery

runner = CliRunner()
TASKS = ["S1-P1-a", "S1-P1-b", "S1-P1-c"]


def tm(root: Path, *args: str) -> Any:
    res = runner.invoke(app, [*args, "-C", str(root)])
    assert res.exit_code == 0, res.output
    return res


def git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


@pytest.fixture(name="root")
def root_fixture(tmp_path: Path) -> Path:
    git(tmp_path, "init", "--bare", "-b", "main", str(tmp_path / "core.git"))
    git(tmp_path, "clone", str(tmp_path / "core.git"), str(tmp_path / "core"))
    work = tmp_path / "core"
    git(work, "config", "user.email", "ci@example.com")
    git(work, "config", "user.name", "CI")
    git(work, "commit", "--allow-empty", "-m", "init")
    git(work, "push", "origin", "HEAD:main")
    git(work, "fetch", "origin")
    tm(tmp_path, "init")
    tm(tmp_path, "spec", "add", "S", "--slug", "S1", "--no-review", "--no-fix")
    tm(tmp_path, "plan", "add", "P", "--spec", "S1", "--slug", "P1")
    for task in TASKS:
        tm(tmp_path, "task", "add", task, "--plan", "S1-P1", "--slug", task[-1])
        tm(tmp_path, "task", "update", task, "--repo", "core")
    tm(tmp_path, "config", "set", "repos.core.gates.main.command", "true")
    return tmp_path


def discover(root: Path, session: str, run: str, slots: int = 9) -> list[str]:
    out = tm(
        root, "wave", "discover", "--session", session, "--reserve", run, "--slots", str(slots)
    ).stdout
    chosen: list[dict[str, Any]] = json.loads(out.split("\n")[0])["chosen"]
    return [c["id"] for c in chosen]


def start(root: Path, node_id: str, session: str) -> None:
    tm(root, "task", "start", node_id, "--agent", f"agent-{node_id}", "--session", session)


def test_two_waves_of_one_session_and_another_session_choose_disjoint_nodes(root: Path) -> None:
    first = discover(root, "S", "r1", slots=2)
    second = discover(root, "S", "r2", slots=3)
    other = discover(root, "T", "r3")

    assert len(first) == 2
    assert len(second) == 1
    assert sorted(first + second) == TASKS
    assert other == []


def test_two_unclaimed_reservations_fill_both_slots_until_a_step_completes(root: Path) -> None:
    held = discover(root, "S", "r1", slots=2)
    assert len(held) == 2

    assert discover(root, "S", "r2", slots=2) == []

    for node_id in held:
        start(root, node_id, "S")
    assert discover(root, "S", "r3", slots=2) == []
    tm(root, "task", "complete", held[0], "--agent", f"agent-{held[0]}")

    freed = discover(root, "S", "r4", slots=2)
    assert len(freed) == 1
    assert freed != [held[1]]


def clock_ahead(monkeypatch: pytest.MonkeyPatch, seconds: int) -> None:
    class Ahead(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> Any:
            return datetime.now(tz=tz) + timedelta(seconds=seconds)

    monkeypatch.setattr(discovery, "datetime", Ahead)


def test_a_reservation_is_ignored_once_600_seconds_pass_on_the_clock(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert sorted(discover(root, "S", "r1", slots=3)) == TASKS

    clock_ahead(monkeypatch, 599)
    assert discover(root, "T", "r2") == []

    clock_ahead(monkeypatch, 601)
    assert sorted(discover(root, "T", "r3")) == TASKS
