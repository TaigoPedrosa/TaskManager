import sys
from pathlib import Path

import pytest
from lifecycle_estate import (
    add,
    attach_landing,
    make_estate,
    on_branch,
    push_main,
    stored,
)

from taskmanager.core.models import GateRun
from taskmanager.core.status import JobState, Outcome, Status
from taskmanager.engine import gates
from taskmanager.engine.claims import Claims
from taskmanager.engine.config import ConfigError, ConfigStore, Gate, ProjectConfig, RepoConfig
from taskmanager.engine.landing import Landing

FAILING = r"^--- FAIL: (\S+)"

# Fails each test the worktree's failing.txt names, printing a go-test-style line per failure
# and writing no report.
GATE_SCRIPT = """
import sys
from pathlib import Path

listed = Path(sys.argv[1]) / "failing.txt"
names = listed.read_text().split() if listed.exists() else []
for name in names:
    print(f"--- FAIL: {name} (0.00s)")
print("FAIL" if names else "ok")
sys.exit(1 if names else 0)
"""


def printing_gate(tmp_path: Path, failing_pattern: str | None) -> Gate:
    script = tmp_path / "gate.py"
    script.write_text(GATE_SCRIPT)
    return Gate(
        command=f"{sys.executable} {script} {{worktree}}",
        failing_pattern=failing_pattern,
        timeout=60,
    )


def landing_on_red_main(
    tmp_path: Path, gate: Gate, on_main: str, on_tip: str
) -> tuple[Claims, Landing, str, JobState]:
    claims = make_estate(
        tmp_path, config=ProjectConfig(repos={"api": RepoConfig(gates={"main": gate})})
    )
    landing = attach_landing(claims)
    api = claims.root / "api"
    push_main(api, "failing.txt", on_main + "\n")
    add(claims, "T1", status=Status.REVIEWED, outcome=Outcome.APPROVE, review_cycles=1)
    on_branch(api, "tm/T1", "feature.py", "x = 1\n")
    if on_tip != on_main:
        on_branch(api, "tm/T1", "failing.txt", on_tip + "\n")
    started = claims.start("T1", "merger", "s1")
    assert started.job is not None, started.reason
    return claims, landing, started.job, landing.run(started.job)


def test_land_with_a_pattern_and_no_new_failure_parks_on_the_red_target(tmp_path: Path) -> None:
    claims, _, _, state = landing_on_red_main(
        tmp_path, printing_gate(tmp_path, FAILING), "a b", "a b"
    )

    assert state == JobState.CONDITION_UNMET
    assert stored(claims, "T1").merge_attempts == 0
    parked = [c for c in claims.nodes.get_conditions("T1") if c.needs.startswith("red-target")]
    assert len(parked) == 1


def test_land_with_a_pattern_and_a_new_failure_is_an_own_defect_naming_it(tmp_path: Path) -> None:
    claims, _, job_id, state = landing_on_red_main(
        tmp_path, printing_gate(tmp_path, FAILING), "a", "a pkg/c"
    )

    assert state == JobState.OWN_DEFECT
    node = stored(claims, "T1")
    assert (node.merge_attempts, node.outcome) == (1, Outcome.MERGE_FAILED)
    job = claims.jobs.get(job_id)
    assert job is not None
    assert "adding pkg/c" in str(job.result)


def test_land_after_a_pattern_is_set_reruns_a_baseline_cached_without_it(tmp_path: Path) -> None:
    gate = printing_gate(tmp_path, FAILING)
    claims = make_estate(
        tmp_path, config=ProjectConfig(repos={"api": RepoConfig(gates={"main": gate})})
    )
    landing = attach_landing(claims)
    api = claims.root / "api"
    sha = push_main(api, "failing.txt", "a b\n")
    unattributed = GateRun(exit_code=1, failing=None, tail="FAIL")
    before = gate.model_copy(update={"failing_pattern": None})
    landing.cache.put_baseline("api", sha, gates.template_hash(before), unattributed)
    add(claims, "T1", status=Status.REVIEWED, outcome=Outcome.APPROVE, review_cycles=1)
    on_branch(api, "tm/T1", "feature.py", "x = 1\n")
    started = claims.start("T1", "merger", "s1")
    assert started.job is not None, started.reason

    assert landing.run(started.job) == JobState.CONDITION_UNMET


@pytest.mark.parametrize(
    "change",
    [
        {"command": "make check"},
        {"junit": "report.xml"},
        {"tests_ran": r"(\d+) passed"},
        {"failing_pattern": r"^FAIL (\S+)"},
    ],
)
def test_template_hash_changes_with_each_field_a_run_depends_on(change: dict[str, str]) -> None:
    gate = Gate(command="make test")
    assert gates.template_hash(gate) != gates.template_hash(gate.model_copy(update=change))


def test_template_hash_ignores_the_timeout() -> None:
    gate = Gate(command="make test")
    assert gates.template_hash(gate) == gates.template_hash(gate.model_copy(update={"timeout": 5}))


def test_land_with_neither_pattern_nor_report_stops_for_an_agent(tmp_path: Path) -> None:
    claims, _, job_id, state = landing_on_red_main(
        tmp_path, printing_gate(tmp_path, None), "a", "a"
    )

    assert state == JobState.NEEDS_AGENT
    job = claims.jobs.get(job_id)
    assert job is not None
    assert job.result["reason"] == "unattributed"


def test_run_gate_collects_each_line_the_pattern_matches_as_an_opaque_id(tmp_path: Path) -> None:
    command = "printf 'ok 1\\n--- FAIL: TestA (0s)\\n--- FAIL: pkg/TestB::x[1] (0s)\\n'; exit 1"
    run = gates.run_gate(command, tmp_path, 10, None, failing_pattern=FAILING)
    assert run.failing == frozenset({"TestA", "pkg/TestB::x[1]"})


def test_run_gate_with_a_pattern_that_captures_nothing_is_unattributable(tmp_path: Path) -> None:
    run = gates.run_gate(
        "echo FAIL; exit 1", tmp_path, 10, None, failing_pattern=r"FAIL(?: (\S+))?"
    )
    assert run.failing is None


def test_run_gate_with_a_report_and_a_pattern_reads_both(tmp_path: Path) -> None:
    report = '<testsuite><testcase classname="s" name="a"><failure/></testcase></testsuite>'
    command = f"printf '%s' '{report}' > report.xml; echo '--- FAIL: b'; exit 1"
    run = gates.run_gate(command, tmp_path, 10, "report.xml", failing_pattern=FAILING)
    assert run.failing == frozenset({"s::a", "b"})


@pytest.mark.parametrize("pattern", ["FAIL", r"(\w+) (\w+)", "(unclosed"])
def test_config_set_failing_pattern_without_exactly_one_group_is_refused(
    tmp_path: Path, pattern: str
) -> None:
    store = ConfigStore(tmp_path)
    store.set("repos.api.gates.main.command", "<your test command>")

    with pytest.raises(ConfigError, match="failing_pattern"):
        store.set("repos.api.gates.main.failing_pattern", pattern)


def test_config_set_failing_pattern_with_one_group_is_stored(tmp_path: Path) -> None:
    store = ConfigStore(tmp_path)
    store.set("repos.api.gates.main.command", "<your test command>")
    store.set("repos.api.gates.main.failing_pattern", FAILING)

    assert store.read()["repos"]["api"]["gates"]["main"]["failing_pattern"] == FAILING
