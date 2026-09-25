from pathlib import Path

import pytest
from lifecycle_estate import git, make_repo, push_main

from taskmanager.core import models
from taskmanager.db.cache_repo import CacheRepository
from taskmanager.db.connection import DatabaseManager
from taskmanager.engine import gates
from taskmanager.engine.gates import GateRun

JUNIT = (
    '<testsuite><testcase classname="s" name="a"><failure/></testcase>'
    '<testcase classname="s" name="b"><error/></testcase>'
    '<testcase classname="s" name="c"/></testsuite>'
)


def test_a_gate_run_is_the_type_the_baseline_cache_stores() -> None:
    assert gates.GateRun is models.GateRun


def test_render_quotes_every_value_spliced_into_the_command() -> None:
    rendered = gates.render("run {worktree} --task-id {node}", worktree="/w t", node="T1")
    assert rendered == "run '/w t' --task-id T1"


def test_the_template_hash_names_the_template_not_its_rendering() -> None:
    assert gates.template_hash("make {worktree}") == gates.template_hash("make {worktree}")
    assert gates.template_hash("make {worktree}") != gates.template_hash("make test {worktree}")


def test_a_green_gate_with_no_report_has_no_failing_set(tmp_path: Path) -> None:
    run = gates.run_gate("echo ok", tmp_path, 10, None)
    assert (run.exit_code, run.failing) == (0, None)
    assert "ok" in run.tail


def test_a_red_gate_reads_its_failing_set_from_junit(tmp_path: Path) -> None:
    command = f"mkdir -p out && printf '%s' '{JUNIT}' > out/report.xml; exit 1"
    run = gates.run_gate(command, tmp_path, 10, "out/*.xml")
    assert run.exit_code == 1
    assert run.failing == frozenset({"s::a", "s::b"})


def test_a_report_left_by_an_earlier_run_is_not_read_as_this_one(tmp_path: Path) -> None:
    (tmp_path / "report.xml").write_text(JUNIT)
    run = gates.run_gate("exit 1", tmp_path, 10, "report.xml")
    assert run.failing is None
    assert not (tmp_path / "report.xml").exists()


def test_a_red_gate_whose_report_names_no_failure_is_unattributable(tmp_path: Path) -> None:
    passing = '<testsuite><testcase classname="s" name="a"/></testsuite>'
    command = f"printf '%s' '{passing}' > report.xml; exit 2"
    assert gates.run_gate(command, tmp_path, 10, "report.xml").failing is None


def test_a_gate_past_its_timeout_is_killed_with_its_children_and_exits_124(tmp_path: Path) -> None:
    run = gates.run_gate("sleep 30 & sleep 30; wait", tmp_path, 1, None)
    assert run.exit_code == 124
    assert "timed out" in run.tail


def _run(code: int, failing: str | None = None) -> GateRun:
    return GateRun(code, None if failing is None else frozenset(failing.split()), "")


@pytest.mark.parametrize(
    ("tip", "base", "verdict"),
    [
        (_run(0), _run(1, "a"), "push"),
        (_run(1, "a"), _run(0, ""), "own_defect"),
        (_run(1), _run(1), "unattributed"),
        (_run(1, "a"), _run(1), "unattributed"),
        (_run(1), _run(1, "a"), "unattributed"),
        (_run(1, "a"), _run(1, "a b"), "push"),
        (_run(1, "a"), _run(1, "a"), "red_target"),
        (_run(1, "a c"), _run(1, "a"), "own_defect"),
        (_run(1, "c"), _run(1, "a"), "own_defect"),
    ],
)
def test_a_red_tip_is_attributed_against_the_baseline(
    tip: GateRun, base: GateRun, verdict: str
) -> None:
    assert gates.attribute(tip, base) == verdict


@pytest.fixture
def red(tmp_path: Path) -> tuple[CacheRepository, Path, str]:
    root = tmp_path / "estate"
    root.mkdir()
    api = make_repo(root, "api")
    db = DatabaseManager(root / ".taskmanager")
    db.init_all()
    return CacheRepository(db), api, git(api, "rev-parse", "origin/main")


PARKED = GateRun(1, frozenset({"s::a"}), "")


@pytest.mark.parametrize(
    ("moves", "later", "cleared"),
    [
        (False, None, False),
        (True, None, True),
        (True, GateRun(0, frozenset(), ""), True),
        (True, GateRun(1, frozenset({"s::a", "s::b"}), ""), False),
        (True, GateRun(1, frozenset({"s::b"}), ""), True),
    ],
)
def test_a_red_target_clears_once_main_moves_past_the_parked_failures(
    red: tuple[CacheRepository, Path, str], moves: bool, later: GateRun | None, cleared: bool
) -> None:
    cache, api, sha = red
    cache.put_baseline("api", sha, "h", PARKED)
    if moves:
        current = push_main(api, "next.txt", "n\n")
        if later is not None:
            cache.put_baseline("api", current, "h", later)
    assert gates.red_target_cleared(cache, api, "api", sha, "h") is cleared


def test_the_red_target_entry_point_exits_zero_only_once_cleared(
    red: tuple[CacheRepository, Path, str], tmp_path: Path
) -> None:
    cache, api, sha = red
    cache.put_baseline("api", sha, "h", PARKED)
    args = ["red-target", "--root", str(tmp_path / "estate"), "--repo", "api", "--sha", sha]
    args += ["--template-hash", "h"]
    assert gates.main(args) == 1
    push_main(api, "next.txt", "n\n")
    assert gates.main(args) == 0
