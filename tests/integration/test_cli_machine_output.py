import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from taskmanager import __version__
from taskmanager.cli.main import app
from taskmanager.core.models import Job, Lease
from taskmanager.core.status import JobKind, JobState
from taskmanager.db.job_repo import JobRepository
from taskmanager.db.runtime_repo import RuntimeRepository
from taskmanager.di.container import create_container

runner = CliRunner()


def _tm(root: Path, *args: str) -> Any:
    res = runner.invoke(app, [*args, "--path", str(root)])
    assert res.exit_code == 0, res.output
    return res


@pytest.fixture
def estate(tmp_path: Path) -> Path:
    _tm(tmp_path, "init")
    _tm(tmp_path, "spec", "add", "Spec", "--slug", "SPEC")
    _tm(tmp_path, "plan", "add", "Plan", "--spec", "SPEC", "--slug", "PLAN")
    _tm(tmp_path, "task", "add", "First", "--plan", "SPEC-PLAN", "--slug", "A")
    _tm(tmp_path, "task", "add", "Second", "--plan", "SPEC-PLAN", "--slug", "B")
    _tm(tmp_path, "task", "depends", "SPEC-PLAN-B", "--add", "SPEC-PLAN-A")
    _tm(tmp_path, "verify", "add", "SPEC-PLAN-A", "--type", "file_exists", "--target", "nope.txt")
    _tm(tmp_path, "decision", "add", "Which?", "--slug", "Q")
    return tmp_path


def _job(root: Path, node_id: str, state: JobState) -> str:
    jobs = create_container(root).get(JobRepository)
    return jobs.create(
        Job(kind=JobKind.LAND, node_id=node_id, repo=".", target="main", state=state)
    ).id


def _lease(root: Path, node_id: str) -> None:
    create_container(root).get(RuntimeRepository).acquire_lease(
        Lease(
            task_id=node_id,
            agent_id="a1",
            session_id="s1",
            branch_name=f"tm/{node_id}",
            ttl_seconds=3600,
            token="tok1",
        ),
        [],
    )


LISTINGS = [
    ("spec", "list"),
    ("plan", "list"),
    ("task", "list"),
    ("decision", "list"),
    ("run", "list"),
    ("job", "list"),
    ("verify", "list", "SPEC-PLAN-A"),
    ("attachments", "SPEC-PLAN-A"),
    ("config", "list"),
    ("audit", "list"),
    ("next",),
    ("spec", "get", "SPEC"),
    ("plan", "get", "SPEC-PLAN"),
    ("task", "get", "SPEC-PLAN-A"),
]


@pytest.mark.parametrize("command", LISTINGS, ids=" ".join)
def test_listing_with_json_and_yaml_parses_to_the_same_document(
    estate: Path, command: tuple[str, ...]
) -> None:
    as_json = json.loads(_tm(estate, *command, "--json").stdout)
    as_yaml = yaml.safe_load(_tm(estate, *command, "--yaml").stdout)
    assert as_json == as_yaml
    assert as_json not in (None, "")


@pytest.mark.parametrize("flag", ["--json", "--yaml"])
def test_verify_run_with_machine_flag_parses_and_keeps_the_failing_exit(
    estate: Path, flag: str
) -> None:
    res = runner.invoke(
        app, ["verify", "run", "SPEC-PLAN-A", "--ref", "HEAD", flag, "--path", str(estate)]
    )
    assert res.exit_code == 1, res.output
    doc = yaml.safe_load(res.stdout)
    assert doc["passed"] is False
    assert doc["results"][0]["verification_type"] == "file_exists"
    assert doc["results"][0]["passed"] is False


def test_version_prints_the_package_version(tmp_path: Path) -> None:
    res = runner.invoke(app, ["--version"])
    assert res.exit_code == 0
    assert res.stdout.strip() == f"tm {__version__}"


def test_spec_get_json_names_its_children(estate: Path) -> None:
    doc = json.loads(_tm(estate, "spec", "get", "SPEC", "--json").stdout)
    assert doc["id"] == "SPEC"
    assert doc["children"] == ["SPEC-PLAN"]


def test_task_list_state_filters_on_the_derived_state(estate: Path) -> None:
    rows = json.loads(_tm(estate, "task", "list", "--state", "BLOCKED_BY_TASK", "--json").stdout)
    assert [r["id"] for r in rows] == ["SPEC-PLAN-B"]
    ready = json.loads(_tm(estate, "task", "list", "--state", "READY", "--json").stdout)
    assert [r["id"] for r in ready] == ["SPEC-PLAN-A"]


def test_spec_and_plan_list_state_filter_on_the_derived_state(estate: Path) -> None:
    for kind in ("spec", "plan"):
        rows = json.loads(_tm(estate, kind, "list", "--state", "STALE", "--json").stdout)
        assert rows == []


def test_job_list_filters_by_node_and_state(estate: Path) -> None:
    live = _job(estate, "SPEC-PLAN-A", JobState.NEEDS_AGENT)
    done = _job(estate, "SPEC-PLAN-A", JobState.SUCCEEDED)
    other = _job(estate, "SPEC-PLAN-B", JobState.RUNNING)

    every = json.loads(_tm(estate, "job", "list", "--json").stdout)
    assert [j["id"] for j in every] == [live, done, other]
    by_node = json.loads(_tm(estate, "job", "list", "--node", "SPEC-PLAN-A", "--json").stdout)
    assert [j["id"] for j in by_node] == [live, done]
    by_state = json.loads(
        _tm(estate, "job", "list", "--state", "succeeded", "--state", "running", "--json").stdout
    )
    assert [j["id"] for j in by_state] == [done, other]


def test_run_list_shows_live_jobs_only(estate: Path) -> None:
    live = _job(estate, "SPEC-PLAN-A", JobState.RUNNING)
    _job(estate, "SPEC-PLAN-A", JobState.EXPIRED)
    doc = json.loads(_tm(estate, "run", "list", "--json").stdout)
    assert [j["id"] for j in doc["jobs"]] == [live]
    assert live in _tm(estate, "run", "list").stdout


def test_task_get_fields_with_yaml_takes_dotted_keys(estate: Path) -> None:
    _lease(estate, "SPEC-PLAN-A")
    res = _tm(estate, "task", "get", "SPEC-PLAN-A", "--yaml", "--fields", "id,lease.agent_id")
    assert yaml.safe_load(res.stdout) == {"id": "SPEC-PLAN-A", "lease.agent_id": "a1"}


def test_config_get_mapping_prints_json(estate: Path) -> None:
    _tm(estate, "config", "set", "repos.app.default_branch", "trunk")
    out = _tm(estate, "config", "get", "repos").stdout
    assert json.loads(out)["app"]["default_branch"] == "trunk"
    listed = _tm(estate, "config", "list").stdout
    line = next(x for x in listed.splitlines() if x.startswith("repos = "))
    value = line.removeprefix("repos = ").rsplit("  (", 1)[0]
    assert json.loads(value)["app"]["default_branch"] == "trunk"


def test_heartbeat_with_another_agent_or_token_is_refused(estate: Path) -> None:
    _lease(estate, "SPEC-PLAN-A")
    for flag, value in (("--agent", "a2"), ("--token", "other")):
        res = runner.invoke(
            app, ["task", "heartbeat", "SPEC-PLAN-A", flag, value, "--path", str(estate)]
        )
        assert res.exit_code == 1, res.output
    _tm(estate, "task", "heartbeat", "SPEC-PLAN-A", "--agent", "a1", "--token", "tok1")
