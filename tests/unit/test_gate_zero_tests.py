from pathlib import Path

import pytest
from lifecycle_estate import add, attach_landing, git, make_estate, on_branch, stored
from test_sync_landing import lagging_parent

from taskmanager.core.status import Action, JobState, Outcome, Status
from taskmanager.engine import gates
from taskmanager.engine.claims import Claims
from taskmanager.engine.config import ConfigError, ConfigStore, Gate, ProjectConfig, RepoConfig

ONE_CASE = '<testsuite><testcase classname="s" name="a"/></testsuite>'
NO_CASE = "<testsuite/>"
COUNT = r"(\d+) passed"
WORD = r"(\w+) passed"


def writes(report: str) -> str:
    return f"printf '%s' '{report}' > report.xml"


@pytest.mark.parametrize(
    ("command", "junit", "tests_ran", "why"),
    [
        (writes(ONE_CASE), "report.xml", None, None),
        (writes(NO_CASE), "report.xml", None, "test report report.xml holds 0 tests"),
        ("true", "out/*.xml", None, "no test report matches out/*.xml"),
        (writes("<testsuite"), "report.xml", None, "test report report.xml could not be read"),
        ("echo 3 passed", None, COUNT, None),
        ("echo 0 passed", None, COUNT, f"the gate ran 0 tests (tests_ran {COUNT!r})"),
        (
            "echo all good",
            None,
            COUNT,
            f"the gate's output never matched tests_ran {COUNT!r} with a count",
        ),
        (
            "echo some passed",
            None,
            WORD,
            f"the gate's output never matched tests_ran {WORD!r} with a count",
        ),
        ("true", None, None, None),
    ],
)
def test_run_gate_with_a_green_exit_names_why_it_ran_no_tests(
    tmp_path: Path, command: str, junit: str | None, tests_ran: str | None, why: str | None
) -> None:
    run = gates.run_gate(command, tmp_path, 10, junit, tests_ran)

    assert (run.exit_code, run.no_tests) == (0, why)


def test_run_gate_with_a_red_exit_is_red_not_no_tests(tmp_path: Path) -> None:
    run = gates.run_gate("echo 0 passed; exit 1", tmp_path, 10, "out/*.xml", COUNT)

    assert (run.exit_code, run.no_tests) == (1, None)


def landing_with(tmp_path: Path, gate: Gate) -> tuple[str, JobState, Claims]:
    claims = make_estate(
        tmp_path, config=ProjectConfig(repos={"api": RepoConfig(gates={"main": gate})})
    )
    landing = attach_landing(claims)
    add(claims, "T1", status=Status.REVIEWED, outcome=Outcome.APPROVE, review_cycles=1)
    on_branch(claims.root / "api", "tm/T1", "feature.py", "x = 1\n")
    result = claims.start("T1", "merger", "s1")
    assert result.action == Action.MERGE, result.reason
    assert result.job is not None
    return result.job, landing.run(result.job), claims


@pytest.mark.parametrize(
    ("gate", "detail"),
    [
        (
            Gate(command=writes(NO_CASE), junit="report.xml"),
            "test report report.xml holds 0 tests",
        ),
        (Gate(command="true", junit="out/*.xml"), "no test report matches out/*.xml"),
        (Gate(command="echo 0 passed", tests_ran=COUNT), "the gate ran 0 tests"),
        (Gate(command="echo all good", tests_ran=COUNT), f"never matched tests_ran {COUNT!r}"),
    ],
)
def test_landing_with_a_gate_that_ran_no_tests_stops_for_an_agent_and_pushes_nothing(
    tmp_path: Path, gate: Gate, detail: str
) -> None:
    job_id, state, claims = landing_with(tmp_path, gate)

    job = claims.jobs.get(job_id)
    assert state == JobState.NEEDS_AGENT
    assert job.result["reason"] == "no tests"
    assert detail in job.result["detail"]
    assert "junit" not in job.result["detail"].lower()
    assert "merge(T1)" not in git(claims.root / "api", "log", "--format=%s", "origin/main")
    assert stored(claims, "T1").status == Status.MERGING


@pytest.mark.parametrize(
    "gate",
    [
        Gate(command=writes(ONE_CASE), junit="report.xml"),
        Gate(command="echo 3 passed", tests_ran=COUNT),
    ],
)
def test_landing_with_a_gate_that_ran_tests_completes(tmp_path: Path, gate: Gate) -> None:
    _, state, claims = landing_with(tmp_path, gate)

    assert state == JobState.SUCCEEDED
    assert stored(claims, "T1").status == Status.COMPLETED


def test_landing_stopped_for_no_tests_resumes_at_the_gate_keeping_the_agents_commit(
    tmp_path: Path,
) -> None:
    gate = Gate(command="test -f fixed.txt && echo 1 passed || echo 0 passed", tests_ran=COUNT)
    job_id, state, claims = landing_with(tmp_path, gate)
    assert state == JobState.NEEDS_AGENT
    handed = claims.start("T1", "resolver", "s2")
    assert (handed.action, handed.job) == (Action.MERGE, job_id)
    assert handed.worktree is not None
    worktree = Path(handed.worktree)
    (worktree / "fixed.txt").write_text("x\n")
    git(worktree, "add", "fixed.txt")
    git(worktree, "commit", "-q", "-m", "add the test the gate counts")

    assert attach_landing(claims).resume(job_id) == JobState.SUCCEEDED
    assert git(claims.root / "api", "show", "origin/main:fixed.txt") == "x"
    assert stored(claims, "T1").status == Status.COMPLETED


def test_sync_with_a_parent_gate_that_ran_no_tests_stops_for_an_agent(tmp_path: Path) -> None:
    gate = Gate(command="echo 0 passed", tests_ran=COUNT)
    claims, landing = lagging_parent(
        tmp_path,
        ProjectConfig(
            repos={"api": RepoConfig(gates={"main": Gate(command="true"), "parent": gate})}
        ),
    )
    first = claims.start("X", "implementer", "s1")
    assert first.job is not None

    assert landing.run(first.job) == JobState.NEEDS_AGENT
    job = claims.jobs.get(first.job)
    assert job is not None and job.result["reason"] == "no tests"


@pytest.mark.parametrize("pattern", ["passed", r"(\d+) of (\d+)", "(unclosed"])
def test_config_set_tests_ran_without_exactly_one_group_is_refused(
    tmp_path: Path, pattern: str
) -> None:
    store = ConfigStore(tmp_path)
    store.set("repos.api.gates.main.command", "<your test command>")

    with pytest.raises(ConfigError, match="tests_ran"):
        store.set("repos.api.gates.main.tests_ran", pattern)


def test_config_set_tests_ran_with_one_group_is_stored(tmp_path: Path) -> None:
    store = ConfigStore(tmp_path)
    store.set("repos.api.gates.main.command", "<your test command>")
    store.set("repos.api.gates.main.tests_ran", COUNT)

    assert store.read()["repos"]["api"]["gates"]["main"]["tests_ran"] == COUNT
