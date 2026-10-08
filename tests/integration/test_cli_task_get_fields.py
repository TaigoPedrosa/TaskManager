import json
from pathlib import Path

from typer.testing import CliRunner

from taskmanager.cli.main import app
from taskmanager.core.models import Job
from taskmanager.core.status import JobKind, JobState
from taskmanager.db.job_repo import JobRepository
from taskmanager.di.container import create_container

runner = CliRunner()


def _make_task(tmp_path: Path) -> str:
    runner.invoke(app, ["init", "--path", str(tmp_path)])
    runner.invoke(app, ["spec", "add", "Spec", "--slug", "SPEC", "--path", str(tmp_path)])
    runner.invoke(
        app, ["plan", "add", "Plan", "--spec", "SPEC", "--slug", "PLAN", "--path", str(tmp_path)]
    )
    res = runner.invoke(
        app, ["task", "add", "Task", "--plan", "SPEC-PLAN", "--path", str(tmp_path)]
    )
    return res.stdout.strip().split()[-1]


def test_task_get_fields_prints_only_the_named_keys(tmp_path: Path) -> None:
    task_id = _make_task(tmp_path)
    res = runner.invoke(
        app,
        [
            "task",
            "get",
            task_id,
            "--json",
            "--fields",
            "status,next_action",
            "--path",
            str(tmp_path),
        ],
    )
    assert res.exit_code == 0
    assert json.loads(res.stdout) == {"status": "READY", "next_action": "implement"}


def test_task_get_without_fields_is_unchanged(tmp_path: Path) -> None:
    task_id = _make_task(tmp_path)
    res = runner.invoke(app, ["task", "get", task_id, "--json", "--path", str(tmp_path)])
    assert res.exit_code == 0
    doc = json.loads(res.stdout)
    assert "jobs" in doc
    assert "sections" in doc


def test_task_get_unknown_field_is_refused_naming_it_and_the_valid_ones(tmp_path: Path) -> None:
    task_id = _make_task(tmp_path)
    res = runner.invoke(
        app, ["task", "get", task_id, "--json", "--fields", "status,bogus", "--path", str(tmp_path)]
    )
    assert res.exit_code == 1
    assert "bogus" in res.stderr
    assert "status" in res.stderr


def test_task_get_fields_without_json_is_refused(tmp_path: Path) -> None:
    task_id = _make_task(tmp_path)
    res = runner.invoke(
        app, ["task", "get", task_id, "--fields", "status", "--path", str(tmp_path)]
    )
    assert res.exit_code == 1


def test_task_get_fields_transcript_stays_small_next_to_a_pytest_studded_job_log(
    tmp_path: Path,
) -> None:
    task_id = _make_task(tmp_path)
    container = create_container(tmp_path)
    job_repo = container.get(JobRepository)
    pytest_log = "\n".join(
        f"FAILED tests/test_x.py::test_{i} - see docs.pytest.org/en/stable/how-to/capture"
        for i in range(80)
    )
    job_repo.create(
        Job(
            kind=JobKind.LAND,
            node_id=task_id,
            repo=".",
            target="main",
            state=JobState.NEEDS_AGENT,
            result={"tail": pytest_log},
        )
    )
    assert len(pytest_log) > 500

    res = runner.invoke(
        app,
        [
            "task",
            "get",
            task_id,
            "--json",
            "--fields",
            "status,next_action,outcome",
            "--path",
            str(tmp_path),
        ],
    )
    assert res.exit_code == 0
    assert len(res.stdout) < 500
