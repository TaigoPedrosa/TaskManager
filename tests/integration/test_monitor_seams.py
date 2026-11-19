"""Rows the real CLI and the real landing engine write, read back through `tm monitor --once`.

A step that must print nothing is followed by a row that always prints (a `config set`), so the
monitor returns at once and the output is that row alone.
"""

import sqlite3
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from taskmanager.cli import main as cli_main
from taskmanager.cli.main import app
from taskmanager.core.status import JobState
from taskmanager.db.utils import to_db_timestamp
from taskmanager.engine.claims import Claims
from taskmanager.engine.landing import Landing

# The estate helpers sit beside the unit tests, with no __init__.py: importable only once their
# directory is on sys.path, which a run of this file alone does not otherwise do.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "unit"))
from lifecycle_estate import attach_landing, push_main

runner = CliRunner()
SENTINEL = "config set config key=verify_timeout"
UPDATED = "task update S2-P1-Y1 priority=60"


def git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.email=ci@example.com", "-c", "user.name=CI", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )


class Clock:
    """The monitor's only wait outside its own settle window is a hang: it found nothing to print."""

    def __init__(self) -> None:
        self.at = datetime.now(tz=UTC)
        self.slept = 0.0

    def now(self) -> datetime:
        return self.at

    def sleep(self, seconds: float) -> None:
        self.slept += seconds
        assert self.slept <= 2.0, "the monitor waited for a row that never printed"
        self.at += timedelta(seconds=seconds)


class Estate:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.after = 0
        self.clock = Clock()

    def tm(self, *args: str) -> str:
        res = runner.invoke(app, [*args, "-C", str(self.root)])
        assert res.exit_code == 0, res.output
        return res.stdout

    def start(self, node: str, agent: str = "ag", session: str = "S") -> dict[str, Any]:
        found: dict[str, Any] = yaml.safe_load(
            self.tm("task", "start", node, "--agent", agent, "--session", session)
        )
        return found

    def implement(self, node: str, name: str | None = None) -> None:
        claim = self.start(node)
        assert claim["action"] == "implement", claim
        worktree = Path(claim["worktree"])
        (worktree / (name or f"{node}.txt")).write_text(node)
        git(worktree, "add", "-A")
        git(worktree, "commit", "-q", "-m", node)
        self.tm("task", "complete", node, "--agent", "ag", "--token", claim["token"])

    def land(self, node: str) -> JobState:
        claim = self.start(node)
        assert claim["action"] == "merge", claim
        return Landing.open(self.root).run(claim["job"])

    def newest(self) -> int:
        events = cli_main._claims(self.root).ops.ledger_repo.after(0)
        return events[-1].id or 0

    def monitor(self, *flags: str, after: int | None = None) -> tuple[list[str], int]:
        self.clock.slept = 0.0
        res = runner.invoke(
            app,
            ["monitor", "--once", "--after", str(self.after if after is None else after)]
            + [*flags, "-C", str(self.root)],
        )
        assert res.exit_code == 0, res.output
        out = res.stdout.splitlines()
        stop = next(i for i, line in enumerate(out) if line.startswith("cursor "))
        return out[:stop], int(out[stop].split()[1])

    def printed(self, *flags: str, sentinel: bool = True) -> list[str]:
        """What the monitor prints for the rows written since the last call."""
        if sentinel:
            self.tm("config", "set", "verify_timeout", "30")
        lines, self.after = self.monitor(*flags)
        if sentinel:
            assert lines.pop() == SENTINEL
        return lines

    def mark(self) -> None:
        self.after = self.newest()

    def plan(self, spec: str = "S1", plan: str = "P1", **tasks: str) -> None:
        """A spec with one plan and a task per keyword, each taking its own add flags."""
        self.tm("spec", "add", spec, "--slug", spec, "--no-review", "--no-fix")
        self.tm("plan", "add", plan, "--spec", spec, "--slug", plan, "--no-review", "--no-fix")
        for slug, flags in tasks.items():
            self.tm(
                "task", "add", slug, "--plan", f"{spec}-{plan}", "--slug", slug, "--repo", "core",
                *flags.split(),
            )  # fmt: skip


@pytest.fixture(name="estate")
def estate_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Estate:
    found = Estate(tmp_path)
    monkeypatch.setattr(cli_main, "SystemClock", lambda: found.clock)
    monkeypatch.setattr(cli_main, "ledger_changes", lambda _claims: lambda: 0)
    git(tmp_path, "init", "--bare", "-b", "main", str(tmp_path / "core.git"))
    git(tmp_path, "clone", str(tmp_path / "core.git"), str(tmp_path / "core"))
    work = tmp_path / "core"
    git(work, "commit", "--allow-empty", "-m", "init")
    git(work, "push", "origin", "HEAD:main")
    git(work, "fetch", "origin")
    found.tm("init")
    found.tm("config", "set", "repos.core.gates.main.command", "true")
    found.tm("config", "set", "repos.core.push", "true")
    return found


def test_a_step_closed_by_complete_review_or_release_prints_one_line_each(estate: Estate) -> None:
    estate.plan(A="--review --fix", B="--no-review --no-fix")
    estate.mark()

    estate.implement("S1-P1-A")
    assert estate.printed() == [
        "task complete S1-P1-A event=complete from=IMPLEMENTING to=IMPLEMENTED"
    ]

    review = estate.start("S1-P1-A", agent="rv")
    assert review["action"] == "review", review
    estate.tm(
        "task",
        "review",
        "S1-P1-A",
        "--approve",
        "--clean",
        "--agent",
        "rv",
        "--token",
        review["token"],
    )
    assert estate.printed() == ["task review S1-P1-A event=approve from=REVIEWING to=REVIEWED"]

    claim = estate.start("S1-P1-B")
    estate.tm("task", "release", "S1-P1-B", "--agent", "ag", "--token", claim["token"])
    assert estate.printed() == ["task release S1-P1-B event=release from=IMPLEMENTING to=READY"]


def test_a_decision_prints_when_it_is_added_and_when_it_is_answered(estate: Estate) -> None:
    estate.plan(A="--no-review --no-fix")
    estate.mark()

    estate.tm(
        "decision",
        "add",
        "Which one?",
        "--slug",
        "d1",
        "--option",
        "a|A|x|none",
        "--blocks",
        "S1-P1-A",
    )
    assert estate.printed() == ['decision_add decision-d1 "Which one?"']

    estate.tm("decision", "answer", "decision-d1", "--option", "a")
    assert estate.printed() == ["decision_answer decision-d1 option=a effect=none affected=[]"]


def test_a_config_set_prints_one_line_in_every_scope(estate: Estate) -> None:
    estate.plan()
    estate.mark()

    estate.tm("config", "set", "verify_timeout", "30")

    assert estate.printed("--spec", "S9", sentinel=False) == [SENTINEL]


def test_a_landing_that_lands_and_the_rollup_it_causes_print_one_line_each(estate: Estate) -> None:
    estate.plan(A="--no-review --no-fix", B="--no-review --no-fix")
    estate.implement("S1-P1-A")
    estate.implement("S1-P1-B")
    estate.mark()

    assert estate.land("S1-P1-A") == JobState.SUCCEEDED
    assert estate.printed() == ["job land S1-P1-A event=landed from=MERGING to=COMPLETED"]

    assert estate.land("S1-P1-B") == JobState.SUCCEEDED
    assert estate.printed() == [
        "rollup S1-P1 from=READY to=IMPLEMENTED",
        "job land S1-P1-B event=landed from=MERGING to=COMPLETED",
    ]


def test_a_landing_that_parks_and_one_that_stops_again_print_one_line_each(estate: Estate) -> None:
    estate.plan(A="--no-review --no-fix --merge spec")
    estate.implement("S1-P1-A", "a.txt")
    push_main(estate.root / "core", "a.txt", "main")
    estate.mark()

    assert estate.land("S1-P1-A") == JobState.NEEDS_AGENT
    parked = estate.printed()
    assert len(parked) == 1
    assert parked[0].startswith("job parked S1-P1-A job=land-")
    assert parked[0].endswith(' "conflict"')

    claim = estate.start("S1-P1-A", agent="fx")
    assert estate.printed() == []

    job = Claims.open(estate.root).jobs.get(claim["job"])
    assert job is not None and job.worktree is not None
    resolved = Path(job.worktree)
    (resolved / "a.txt").write_text("resolved")
    git(resolved, "add", "-A")
    git(resolved, "commit", "-q", "-m", "resolve")
    estate.tm("config", "set", "repos.core.gates.main.command", "false")
    estate.mark()
    landing = attach_landing(Claims.open(estate.root, actor="fx"))

    assert landing.resume(claim["job"], agent="fx", token=claim["token"]) == JobState.NEEDS_AGENT
    assert estate.printed() == ["job stopped again S1-P1-A step_failures=1"]


def test_a_lease_sweep_prints_one_line_and_never_to_the_session_that_ran_it(estate: Estate) -> None:
    estate.plan(A="--no-review --no-fix")
    estate.start("S1-P1-A")
    expired = datetime.now(tz=UTC) - timedelta(days=1)
    with sqlite3.connect(estate.root / ".taskmanager" / "state.db") as db:
        moved = db.execute(
            "UPDATE leases SET last_heartbeat = ? WHERE task_id = 'S1-P1-A'",
            (to_db_timestamp(expired),),
        ).rowcount
    assert moved == 1
    estate.mark()
    before = estate.after

    estate.tm("wave", "discover", "--session", "W")
    estate.tm("config", "set", "verify_timeout", "30")

    own, _ = estate.monitor("--session", "W", after=before)
    other, _ = estate.monitor("--session", "V", after=before)
    assert own == [SENTINEL]
    assert other == ["lease sweep S1-P1-A event=expired from=IMPLEMENTING to=READY", SENTINEL]


def test_an_overdue_lease_prints_once_in_a_batch_and_not_again_on_the_next_invocation(
    estate: Estate,
) -> None:
    estate.plan(A="--no-review --no-fix")
    estate.start("S1-P1-A")
    expired = datetime.now(tz=UTC) - timedelta(days=1)
    with sqlite3.connect(estate.root / ".taskmanager" / "state.db") as db:
        moved = db.execute(
            "UPDATE leases SET last_heartbeat = ? WHERE task_id = 'S1-P1-A'",
            (to_db_timestamp(expired),),
        ).rowcount
    assert moved == 1
    estate.mark()

    estate.tm("config", "set", "verify_timeout", "30")
    first, estate.after = estate.monitor("--session", "V")
    assert first == [SENTINEL, "lease S1-P1-A implement overdue"]
    assert estate.printed("--session", "V") == []


def test_a_condition_flip_prints_one_line(estate: Estate) -> None:
    estate.plan(A="--no-review --no-fix")
    estate.tm("config", "set", "condition_ttl", "0")
    estate.tm(
        "task", "condition", "add", "S1-P1-A", "--needs", "ready", "--command", "test -f ready"
    )
    blocked = runner.invoke(
        app, ["task", "start", "S1-P1-A", "--agent", "ag", "--session", "S", "-C", str(estate.root)]
    )
    assert blocked.exit_code == 3, blocked.output
    estate.mark()

    (estate.root / "ready").write_text("x")
    estate.start("S1-P1-A")

    assert estate.printed() == ['condition met S1-P1-A idx=1 "ready"']


def test_what_an_agent_writes_while_it_works_prints_nothing(estate: Estate) -> None:
    estate.plan(A="--review --fix", B="--no-review --no-fix", C="--no-review --no-fix")
    git(
        estate.root / "core",
        "worktree",
        "add",
        "-q",
        "-b",
        "other",
        str(estate.root / ".worktrees" / "core-S1-P1-C"),
        "origin/main",
    )
    estate.implement("S1-P1-A")
    estate.mark()

    claim = estate.start("S1-P1-B")
    token = ("--agent", "ag", "--token", claim["token"])
    estate.tm("task", "heartbeat", "S1-P1-B")
    estate.tm("section", "set", "S1-P1-B:x", "--content", "x")
    estate.tm("note", "add", "S1-P1-B", "a note", *token)
    estate.tm("check", "add", "S1-P1-B", "--deleted", "a.py:1", "--green", *token)
    undone = runner.invoke(
        app, ["task", "start", "S1-P1-C", "--agent", "ag", "--session", "S", "-C", str(estate.root)]
    )
    assert "claim of S1-P1-C undone" in undone.output, undone.output
    review = estate.start("S1-P1-A", agent="rv")
    estate.tm(
        "finding",
        "add",
        "S1-P1-A",
        "--at",
        "a.py:f",
        "--breaks",
        "it breaks",
        "--minor",
        "--agent",
        "rv",
        "--token",
        review["token"],
    )

    assert estate.printed() == []


def test_after_a_spec_is_archived_and_restored_only_the_new_rows_print(estate: Estate) -> None:
    estate.plan(A="--no-review --no-fix")
    estate.implement("S1-P1-A")
    for node in ("S1-P1-A", "S1-P1", "S1"):
        assert estate.land(node) == JobState.SUCCEEDED
    estate.mark()

    estate.tm("spec", "archive", "S1")
    estate.tm("config", "set", "verify_timeout", "30")
    with sqlite3.connect(estate.root / ".taskmanager" / "archive.db") as db:
        aged = db.execute(
            "UPDATE ledger_events SET timestamp = ?",
            (to_db_timestamp(datetime.now(tz=UTC) - timedelta(days=1)),),
        ).rowcount
    assert aged > 0
    estate.tm("spec", "unarchive", "S1")
    estate.tm("config", "set", "verify_timeout", "31")

    assert estate.printed(sentinel=False) == [SENTINEL, SENTINEL]


def scoped(estate: Estate, before: int, *flags: str) -> list[str]:
    lines, _ = estate.monitor(*flags, after=before)
    return [line for line in lines if line != SENTINEL]


def test_a_scoped_monitor_prints_another_specs_move_only_when_one_of_its_nodes_waits_on_it(
    estate: Estate,
) -> None:
    estate.plan(
        "S2",
        Y1="--no-review --no-fix",
        Y2='--no-review --no-fix --set declared_files=["shared.py"]',
        Y3="--no-review --no-fix",
    )
    estate.plan(
        "S1",
        X1='--no-review --no-fix --set declared_files=["shared.py"]',
        X2="--no-review --no-fix --depends-on S2-P1-Y1",
        X3="--no-review --no-fix",
    )
    estate.mark()
    before = estate.after

    estate.implement("S2-P1-Y3")
    estate.implement("S2-P1-Y1")
    estate.implement("S2-P1-Y2", "shared.py")
    estate.implement("S1-P1-X3")
    estate.tm("task", "update", "S2-P1-Y1", "--priority", "60")
    estate.tm("config", "set", "verify_timeout", "30")

    done = "task complete {} event=complete from=IMPLEMENTING to=IMPLEMENTED"
    assert scoped(estate, before, "--spec", "S1") == [
        done.format("S2-P1-Y1"),
        done.format("S2-P1-Y2"),
        done.format("S1-P1-X3"),
    ]
    assert scoped(estate, before, "--spec", "S2") == [
        done.format("S2-P1-Y3"),
        done.format("S2-P1-Y1"),
        done.format("S2-P1-Y2"),
        UPDATED,
    ]
    assert scoped(estate, before) == [
        *(done.format(node) for node in ("S2-P1-Y3", "S2-P1-Y1", "S2-P1-Y2", "S1-P1-X3")),
        UPDATED,
    ]


def test_a_decision_tied_to_no_node_prints_unscoped_or_to_the_session_that_raised_it(
    estate: Estate,
) -> None:
    estate.plan("S1", X1="--no-review --no-fix")
    estate.plan("S2", Y1="--no-review --no-fix")
    estate.mark()
    before = estate.after

    estate.tm("decision", "add", "Loose?", "--slug", "loose", "--option", "a|A|x|none")
    estate.tm("decision", "add", "Raised?", "--slug", "raised", "--option", "a|A|x|none")
    nodes = Claims.open(estate.root).nodes
    decision = nodes.get_node("decision-raised")
    assert decision is not None
    data = {**decision.frontmatter["decision"], "raised_by": "wave-1"}
    nodes.save_node(decision.model_copy(update={"frontmatter": {"decision": data}}))
    estate.tm(
        "decision",
        "add",
        "Mine?",
        "--slug",
        "mine",
        "--option",
        "a|A|x|none",
        "--blocks",
        "S1-P1-X1",
    )
    estate.tm(
        "decision",
        "add",
        "Theirs?",
        "--slug",
        "theirs",
        "--option",
        "a|A|x|none",
        "--blocks",
        "S2-P1-Y1",
    )
    estate.tm("config", "set", "verify_timeout", "30")

    mine, theirs = 'decision_add decision-mine "Mine?"', 'decision_add decision-theirs "Theirs?"'
    assert scoped(estate, before, "--spec", "S1", "--session", "wave-2") == [mine]
    assert scoped(estate, before, "--spec", "S2", "--session", "wave-2") == [theirs]
    loose, raised = 'decision_add decision-loose "Loose?"', 'decision_add decision-raised "Raised?"'
    assert scoped(estate, before) == [loose, raised, mine, theirs]
    assert scoped(estate, before, "--spec", "S1") == [mine]
    assert scoped(estate, before, "--spec", "S1", "--session", "wave-1") == [raised, mine]
