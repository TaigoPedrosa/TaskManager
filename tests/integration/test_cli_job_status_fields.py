import json
from pathlib import Path

from typer.testing import CliRunner

from taskmanager.cli.main import app
from taskmanager.core.models import Job
from taskmanager.core.status import JobKind, JobState
from taskmanager.db.job_repo import JobRepository
from taskmanager.di.container import create_container

runner = CliRunner()


def _make_job(tmp_path: Path, **overrides: object) -> str:
    runner.invoke(app, ["init", "--path", str(tmp_path)])
    runner.invoke(app, ["spec", "add", "Spec", "--slug", "SPEC", "--path", str(tmp_path)])
    runner.invoke(
        app, ["plan", "add", "Plan", "--spec", "SPEC", "--slug", "PLAN", "--path", str(tmp_path)]
    )
    res = runner.invoke(
        app, ["task", "add", "Task", "--plan", "SPEC-PLAN", "--path", str(tmp_path)]
    )
    node_id = res.stdout.strip().split()[-1]
    container = create_container(tmp_path)
    job_repo = container.get(JobRepository)
    kwargs: dict[str, object] = {
        "kind": JobKind.LAND,
        "node_id": node_id,
        "repo": ".",
        "target": "main",
        "state": JobState.SUCCEEDED,
        "step": "gate",
        "worktree": "/est/.worktrees/land-T1",
        "result": {"next": None, "resumed": 0},
    }
    kwargs.update(overrides)
    job = job_repo.create(Job(**kwargs))  # type: ignore[arg-type]
    return job.id


def test_job_status_fields_prints_only_the_named_keys(tmp_path: Path) -> None:
    job_id = _make_job(tmp_path, result={"next": "J2", "resumed": 0})
    res = runner.invoke(
        app, ["job", "status", job_id, "--fields", "state,result.next", "--path", str(tmp_path)]
    )
    assert res.exit_code == 0
    assert json.loads(res.stdout) == {"state": "succeeded", "result.next": "J2"}


def test_job_status_without_fields_is_unchanged(tmp_path: Path) -> None:
    job_id = _make_job(tmp_path)
    res = runner.invoke(app, ["job", "status", job_id, "--path", str(tmp_path)])
    assert res.exit_code == 0
    doc = json.loads(res.stdout)
    assert "worktree" in doc
    assert "pid" in doc


def test_job_status_unknown_field_is_refused_naming_it_and_the_valid_ones(tmp_path: Path) -> None:
    job_id = _make_job(tmp_path)
    res = runner.invoke(
        app, ["job", "status", job_id, "--fields", "state,bogus", "--path", str(tmp_path)]
    )
    assert res.exit_code == 1
    assert "bogus" in res.stderr
    assert "state" in res.stderr


def test_job_status_nested_field_a_run_never_populated_is_null_not_unknown(tmp_path: Path) -> None:
    job_id = _make_job(tmp_path, state=JobState.NEEDS_AGENT, result={"tail": "conflict"})
    res = runner.invoke(
        app,
        [
            "job",
            "status",
            job_id,
            "--fields",
            "state,result.next,result.resumed",
            "--path",
            str(tmp_path),
        ],
    )
    assert res.exit_code == 0
    assert json.loads(res.stdout) == {
        "state": "needs_agent",
        "result.next": None,
        "result.resumed": None,
    }


def test_job_status_fields_transcript_stays_small_next_to_a_pytest_studded_result(
    tmp_path: Path,
) -> None:
    pytest_log = "\n".join(
        f"FAILED tests/test_x.py::test_{i} - see docs.pytest.org/en/stable/how-to/capture"
        for i in range(80)
    )
    job_id = _make_job(
        tmp_path,
        state=JobState.NEEDS_AGENT,
        result={"tail": pytest_log, "next": None, "resumed": 1},
    )
    assert len(pytest_log) > 500

    res = runner.invoke(
        app,
        [
            "job",
            "status",
            job_id,
            "--fields",
            "state,kind,repo,target,step,worktree,result.next,result.resumed",
            "--path",
            str(tmp_path),
        ],
    )
    assert res.exit_code == 0
    assert len(res.stdout) < 500
