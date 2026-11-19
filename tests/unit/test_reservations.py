import json
import sqlite3
from collections.abc import Callable
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from lifecycle_estate import add, gated, make_estate, stored
from test_discovery import hold, waiting_job
from typer.testing import CliRunner

from taskmanager.cli.main import app
from taskmanager.core.enums import NodeKind
from taskmanager.core.models import Condition, Job, Lease, Node
from taskmanager.core.status import (
    Action,
    ConditionStage,
    DecisionStatus,
    JobKind,
    JobState,
    Outcome,
    Status,
)
from taskmanager.db.cache_repo import CacheRepository
from taskmanager.db.utils import parse_db_datetime
from taskmanager.engine import selection
from taskmanager.engine.archive import archive_spec
from taskmanager.engine.claims import Claims
from taskmanager.engine.discovery import discover
from taskmanager.engine.operations import OperationError

STRONG = "claude-opus-5-5"
_LONG_AGO = datetime(2000, 1, 1, tzinfo=UTC)
runner = CliRunner()


def batch(claims: Claims, **args: Any) -> dict[str, Any]:
    options: dict[str, Any] = {"specs": None, "session": "s1", "slots": 10, "max_strong": 10}
    options.update(args)
    payload, _ = discover(claims, **options)
    data: dict[str, Any] = json.loads(payload)
    return data


def chosen(data: dict[str, Any]) -> list[str]:
    return [entry["id"] for entry in data["chosen"]]


def reservations(claims: Claims) -> dict[str, tuple[str, datetime]]:
    # Its own connection, so a write left uncommitted on the engine's is not read back.
    with closing(sqlite3.connect(claims.runtime.db.state_db)) as conn:
        rows = conn.execute("SELECT node_id, session_id, expires_at FROM reservations").fetchall()
    return {node_id: (session, parse_db_datetime(at)) for node_id, session, at in rows}


def ahead(seconds: int = 300) -> datetime:
    return datetime.now(tz=UTC) + timedelta(seconds=seconds)


def test_a_reserving_discover_holds_each_chosen_id_for_its_session_600_seconds_ahead(
    tmp_path: Path,
) -> None:
    claims = make_estate(tmp_path)
    add(claims, "T1", priority=90)
    add(claims, "T2", priority=80)
    add(claims, "T3", priority=70)

    before = datetime.now(tz=UTC)
    data = batch(claims, slots=2, reserve="r1")
    after = datetime.now(tz=UTC)

    assert chosen(data) == ["T1", "T2"]
    held = reservations(claims)
    assert sorted(held) == ["T1", "T2"]
    for session, expires_at in held.values():
        assert session == "s1"
        assert before + timedelta(seconds=600) <= expires_at <= after + timedelta(seconds=600)


def test_a_discover_without_reserve_writes_no_reservation(tmp_path: Path) -> None:
    claims = make_estate(tmp_path)
    add(claims, "T1")

    assert chosen(batch(claims)) == ["T1"]
    assert reservations(claims) == {}


def test_leases_and_reservations_are_read_and_the_reservations_written_in_one_immediate_transaction(
    tmp_path: Path,
) -> None:
    claims = make_estate(tmp_path)
    add(claims, "T1")
    statements: list[str] = []
    with claims.runtime.db.get_state_connection() as conn:
        conn.set_trace_callback(statements.append)
        try:
            batch(claims, reserve="r1")
        finally:
            conn.set_trace_callback(None)

    [insert_at] = [
        i for i, s in enumerate(statements) if s.startswith("INSERT OR REPLACE INTO reservations")
    ]
    begin_at = max(i for i, s in enumerate(statements[:insert_at]) if s == "BEGIN IMMEDIATE")
    held = statements[begin_at:insert_at]
    assert not [s for s in held if s.startswith(("COMMIT", "ROLLBACK"))]
    assert any("FROM leases" in s for s in held)
    [delete_at] = [i for i, s in enumerate(held) if s.startswith("DELETE FROM reservations")]
    assert "run_id = 'r1'" in held[delete_at]
    read_at = min(
        i for i, s in enumerate(held) if s.startswith("SELECT") and "FROM reservations" in s
    )
    assert delete_at < read_at
    ends = [s for s in statements[insert_at:] if s.startswith(("COMMIT", "ROLLBACK"))]
    assert ends[0] == "COMMIT"


def test_a_discover_racing_one_that_has_selected_cannot_read_until_it_has_reserved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = make_estate(tmp_path)
    add(first, "T1", priority=90)
    add(first, "T2", priority=80)
    second = Claims.open(first.root, first.config)
    # Refused at once rather than after the busy timeout, so the race is decided in this thread.
    with second.runtime.db.get_state_connection() as conn:
        conn.execute("PRAGMA busy_timeout = 0")
    real_select = selection.select
    selects = 0

    def select_then_race(*args: Any, **kwargs: Any) -> selection.Selection:
        nonlocal selects
        selects += 1
        result = real_select(*args, **kwargs)
        if selects == 1:
            with pytest.raises(sqlite3.OperationalError, match="database is locked"):
                batch(second, session="s2", slots=1, reserve="r2")
        return result

    monkeypatch.setattr(selection, "select", select_then_race)

    assert chosen(batch(first, slots=1, reserve="r1")) == ["T1"]
    assert chosen(batch(second, session="s2", slots=1, reserve="r2")) == ["T2"]
    assert {node_id: held[0] for node_id, held in reservations(first).items()} == {
        "T1": "s1",
        "T2": "s2",
    }


def test_a_second_discover_of_one_run_replaces_its_reservations_and_leaves_another_runs(
    tmp_path: Path,
) -> None:
    claims = make_estate(tmp_path)
    add(claims, "T1", priority=90)
    add(claims, "T2", priority=80)
    add(claims, "T3", priority=70)
    assert chosen(batch(claims, session="s2", slots=1, reserve="other")) == ["T1"]
    assert chosen(batch(claims, slots=1, reserve="r1")) == ["T2"]

    assert chosen(batch(claims, slots=1, reserve="r1")) == ["T2"]

    with closing(sqlite3.connect(claims.runtime.db.state_db)) as conn:
        rows = conn.execute("SELECT node_id, session_id, run_id FROM reservations").fetchall()
    assert sorted(rows) == [("T1", "s2", "other"), ("T2", "s1", "r1")]


@pytest.mark.parametrize("session", ["s1", "s2"])
def test_a_second_discover_in_any_session_never_chooses_a_reserved_id(
    tmp_path: Path, session: str
) -> None:
    claims = make_estate(tmp_path)
    add(claims, "T1", priority=90)
    add(claims, "T2", priority=80)
    assert chosen(batch(claims, slots=1, reserve="r1")) == ["T1"]

    assert chosen(batch(claims, session=session, slots=5)) == ["T2"]


def test_a_reserved_id_takes_its_declared_files_from_the_batch_and_is_not_held_as_excluded(
    tmp_path: Path,
) -> None:
    claims = make_estate(tmp_path)
    add(claims, "T1", files=["api/a.py"], priority=90)
    add(claims, "T2", files=["api/a.py"], priority=80)
    add(claims, "T3", priority=70)
    claims.runtime.reserve(["T1"], "s2", "r1", ahead())

    data = batch(claims)

    assert chosen(data) == ["T3"]
    assert data["held"] == ["T2: declared_files overlap a node chosen this wave"]


def test_a_reservation_on_a_node_archived_since_still_fills_its_sessions_slot(
    tmp_path: Path,
) -> None:
    claims = make_estate(tmp_path)
    add(claims, "S", NodeKind.SPEC, status=Status.COMPLETED)
    add(claims, "S-T", parent="S", status=Status.COMPLETED)
    claims.runtime.reserve(["S-T"], "s1", "r1", ahead())
    archive_spec(claims.root, "S")
    add(claims, "T1", priority=90)
    add(claims, "T2", priority=80)

    assert chosen(batch(claims, slots=2)) == ["T1"]


def test_an_expired_reservation_neither_hides_its_node_nor_fills_a_slot(tmp_path: Path) -> None:
    claims = make_estate(tmp_path)
    add(claims, "T1")
    claims.runtime.reserve(["T1"], "s1", "r1", datetime.now(tz=UTC) - timedelta(seconds=1))

    assert chosen(batch(claims, slots=1)) == ["T1"]


def test_a_sweep_deletes_every_expired_reservation_and_keeps_the_live_ones(tmp_path: Path) -> None:
    claims = make_estate(tmp_path)
    add(claims, "T1")
    add(claims, "T2")
    claims.runtime.reserve(["T1"], "s1", "r1", datetime.now(tz=UTC) - timedelta(seconds=1))
    claims.runtime.reserve(["T2"], "s2", "r2", ahead())

    claims.sweep()

    assert sorted(reservations(claims)) == ["T2"]


def test_a_sessions_reservation_on_a_node_it_holds_no_lease_on_fills_one_of_its_slots(
    tmp_path: Path,
) -> None:
    claims = make_estate(tmp_path)
    add(claims, "T1")
    add(claims, "T2")
    claims.runtime.reserve(["T1"], "s1", "r1", ahead())

    data = batch(claims, slots=1)

    assert (chosen(data), data["waiting_for_slot"]) == ([], 1)


def test_another_sessions_reservation_fills_none_of_this_sessions_slots(tmp_path: Path) -> None:
    claims = make_estate(tmp_path)
    add(claims, "T1")
    add(claims, "T2")
    claims.runtime.reserve(["T1"], "s2", "r1", ahead())

    assert chosen(batch(claims, slots=1)) == ["T2"]


def test_a_reservation_on_a_node_the_session_holds_a_lease_on_fills_no_second_slot(
    tmp_path: Path,
) -> None:
    claims = make_estate(tmp_path)
    add(claims, "T0")
    node = stored(claims, "T0")
    lease = Lease(
        task_id="T0",
        agent_id="agent-T0",
        session_id="s1",
        branch_name="tm/T0",
        ttl_seconds=3600,
        action=Action.IMPLEMENT,
    )
    moved = node.model_copy(update={"status": Status.IMPLEMENTING, "claimed_from": node.status})
    assert claims.runtime.claim(lease, [], moved)
    claims.runtime.reserve(["T0"], "s1", "r1", ahead())
    add(claims, "T1")

    assert chosen(batch(claims, slots=2)) == ["T1"]


def test_a_reservation_on_a_strong_model_node_fills_one_strong_slot(tmp_path: Path) -> None:
    claims = make_estate(tmp_path)
    add(claims, "T1", models=[STRONG], priority=90)
    add(claims, "T2", models=[STRONG], priority=80)
    add(claims, "T3", priority=70)
    claims.runtime.reserve(["T1"], "s1", "r1", ahead())

    data = batch(claims, max_strong=1)

    assert chosen(data) == ["T3"]
    assert "T2: no free strong-model slot" in data["held"]


def test_a_claim_that_succeeds_deletes_the_nodes_reservation(tmp_path: Path) -> None:
    claims = make_estate(tmp_path)
    add(claims, "T1")
    claims.runtime.reserve(["T1"], "s1", "r1", ahead())

    assert claims.start("T1", "agent-1", "s1").action == Action.IMPLEMENT
    assert reservations(claims) == {}


def test_a_blocked_claim_deletes_the_nodes_reservation(tmp_path: Path) -> None:
    claims = make_estate(tmp_path)
    add(claims, "T2")
    add(claims, "T1", depends=("T2",))
    claims.runtime.reserve(["T1"], "s1", "r1", ahead())

    assert claims.start("T1", "agent-1", "s1").action == Action.BLOCKED
    assert reservations(claims) == {}


def test_a_refused_claim_deletes_the_nodes_reservation(tmp_path: Path) -> None:
    claims = make_estate(tmp_path)
    claims.nodes.save_node(
        Node(id="D1", kind=NodeKind.DECISION, title="d", status=DecisionStatus.OPEN)
    )
    claims.runtime.reserve(["D1"], "s1", "r1", ahead())

    with pytest.raises(OperationError):
        claims.start("D1", "agent-1", "s1")
    assert reservations(claims) == {}


def _new_ledger_rows(root: Path, after: int) -> list[tuple[str, str]]:
    conn = sqlite3.connect(root / ".taskmanager" / "audit.db")
    try:
        return conn.execute(
            "SELECT actor_id, command FROM ledger_events WHERE id > ? ORDER BY id", (after,)
        ).fetchall()
    finally:
        conn.close()


def _last_ledger_id(root: Path) -> int:
    conn = sqlite3.connect(root / ".taskmanager" / "audit.db")
    try:
        return int(conn.execute("SELECT COALESCE(MAX(id), 0) FROM ledger_events").fetchone()[0])
    finally:
        conn.close()


def test_tm_wave_discover_records_its_sweep_red_target_decision_and_condition_flip_as_the_session(
    tmp_path: Path,
) -> None:
    claims = make_estate(tmp_path, config=gated("api").model_copy(update={"condition_ttl": 0}))
    root = claims.root
    add(claims, "T0")
    node = stored(claims, "T0")
    lapsed = Lease(
        task_id="T0",
        agent_id="agent-T0",
        session_id="s0",
        branch_name="tm/T0",
        ttl_seconds=1,
        acquired_at=_LONG_AGO,
        last_heartbeat=_LONG_AGO,
        action=Action.IMPLEMENT,
    )
    moved = node.model_copy(update={"status": Status.IMPLEMENTING, "claimed_from": node.status})
    assert claims.runtime.claim(lapsed, [], moved)
    sha = "a" * 40
    add(claims, "TR")
    claims.nodes.add_condition(
        Condition(
            node_id="TR",
            idx=0,
            needs=f"red-target: api main at {sha[:12]} fails the 1 test(s)",
            command="true",
            stage=ConditionStage.LANDING,
        )
    )
    mark = {
        "repo": "api",
        "target": "main",
        "sha": sha,
        "since": "2000-01-01T00:00:00+00:00",
        "failing": ["suite.t"],
    }
    claims.jobs.create(
        Job(
            kind=JobKind.LAND,
            node_id="TR",
            repo="api",
            target="main",
            state=JobState.EXPIRED,
            result={"red_target": mark},
        )
    )
    add(claims, "TC")
    condition = claims.nodes.add_condition(
        Condition(node_id="TC", needs="the build is ready", command="test -f ready")
    )
    CacheRepository(claims.nodes.db).put_condition("TC", condition.idx, "test -f ready", 1)
    (root / "ready").write_text("x", encoding="utf-8")
    after = _last_ledger_id(root)

    result = runner.invoke(app, ["wave", "discover", "--session", "S", "-C", str(root)])

    assert result.exit_code == 0, result.output
    rows = _new_ledger_rows(root, after)
    assert {"lease sweep", "decision_add", "condition met"} <= {command for _, command in rows}
    assert {actor for actor, _ in rows} == {"S"}
    assert reservations(claims) == {}


def test_tm_wave_discover_with_reserve_reserves_what_it_chose_for_the_session(
    tmp_path: Path,
) -> None:
    claims = make_estate(tmp_path)
    add(claims, "T1")

    result = runner.invoke(
        app, ["wave", "discover", "--session", "S", "--reserve", "R", "-C", str(claims.root)]
    )

    assert result.exit_code == 0, result.output
    with closing(sqlite3.connect(claims.runtime.db.state_db)) as conn:
        rows = conn.execute("SELECT node_id, session_id, run_id FROM reservations").fetchall()
    assert rows == [("T1", "S", "R")]


@pytest.mark.parametrize("run", ["-r", ".r", "r/1", "r 1", "r;x"])
def test_tm_wave_discover_refuses_a_run_id_that_is_not_a_plain_name(
    tmp_path: Path, run: str
) -> None:
    claims = make_estate(tmp_path)
    add(claims, "T1")

    result = runner.invoke(
        app, ["wave", "discover", "--session", "S", f"--reserve={run}", "-C", str(claims.root)]
    )

    assert result.exit_code == 2, result.output
    assert reservations(claims) == {}


def _after_candidates(monkeypatch: pytest.MonkeyPatch, between: Callable[[], None]) -> None:
    """Runs `between` once discover has its candidates and before its transaction opens."""
    real = selection.candidates

    def then(*args: Any, **kwargs: Any) -> tuple[list[selection.Candidate], list[str]]:
        found = real(*args, **kwargs)
        between()
        return found

    monkeypatch.setattr(selection, "candidates", then)


def test_a_claim_another_session_commits_after_candidates_keeps_its_node_out_of_the_batch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    claims = make_estate(tmp_path)
    add(claims, "T1", priority=90)
    add(claims, "T2", priority=80)
    other = Claims.open(claims.root, claims.config)

    def claim_t1() -> None:
        assert other.start("T1", "agent-2", "s2").action == Action.IMPLEMENT

    _after_candidates(monkeypatch, claim_t1)

    assert chosen(batch(claims, slots=1, reserve="r1")) == ["T2"]
    assert {node_id: held[0] for node_id, held in reservations(claims).items()} == {"T2": "s1"}


def test_a_lease_parked_after_candidates_on_a_node_with_no_waiting_job_keeps_it_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    claims = make_estate(tmp_path)
    add(claims, "T1", priority=90)
    add(claims, "T2", priority=80)

    def claim_then_park() -> None:
        hold(claims, "T1", "s2", Status.IMPLEMENTING, Action.IMPLEMENT)
        claims.runtime.park("T1")

    _after_candidates(monkeypatch, claim_then_park)

    assert chosen(batch(claims, slots=1)) == ["T2"]


def test_a_stopped_landing_whose_parked_lease_is_untouched_is_still_offered_for_hand_over(
    tmp_path: Path,
) -> None:
    claims = make_estate(tmp_path, config=gated("api"))
    add(claims, "T1", status=Status.REVIEWED, outcome=Outcome.APPROVE)
    hold(claims, "T1", "s2", Status.MERGING, Action.MERGE)
    job = waiting_job(claims, "T1", JobKind.LAND)

    data = batch(claims)

    assert [(entry["id"], entry["job"]) for entry in data["chosen"]] == [("T1", job)]


def test_a_stopped_landing_another_session_takes_over_after_candidates_is_not_offered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    claims = make_estate(tmp_path, config=gated("api"))
    add(claims, "T1", status=Status.REVIEWED, outcome=Outcome.APPROVE)
    hold(claims, "T1", "s2", Status.MERGING, Action.MERGE)
    waiting_job(claims, "T1", JobKind.LAND)

    def take_over() -> None:
        assert claims.runtime.take_over("T1", "agent-2", "s2", 3600, STRONG, "k2")

    _after_candidates(monkeypatch, take_over)

    assert chosen(batch(claims)) == []


def test_a_lease_already_expired_when_the_transaction_reads_it_holds_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    claims = make_estate(tmp_path)
    add(claims, "T1")

    def lapsed_claim() -> None:
        node = stored(claims, "T1")
        lease = Lease(
            task_id="T1",
            agent_id="agent-T1",
            session_id="s2",
            branch_name="tm/T1",
            ttl_seconds=1,
            acquired_at=_LONG_AGO,
            last_heartbeat=_LONG_AGO,
            action=Action.IMPLEMENT,
        )
        moved = node.model_copy(update={"status": Status.IMPLEMENTING, "claimed_from": node.status})
        assert claims.runtime.claim(lease, [], moved)

    _after_candidates(monkeypatch, lapsed_claim)

    assert chosen(batch(claims)) == ["T1"]


def test_tm_wave_unreserve_deletes_only_the_sessions_reservations_on_the_ids_it_names(
    tmp_path: Path,
) -> None:
    claims = make_estate(tmp_path)
    claims.runtime.reserve(["T1", "T2"], "S", "r1", ahead())
    claims.runtime.reserve(["T3"], "S2", "r1", ahead())

    result = runner.invoke(
        app, ["wave", "unreserve", "--session", "S", "T1", "T3", "-C", str(claims.root)]
    )

    assert result.exit_code == 0, result.output
    assert {node_id: held[0] for node_id, held in reservations(claims).items()} == {
        "T2": "S",
        "T3": "S2",
    }


def test_tm_wave_unreserve_with_nothing_reserved_exits_0(tmp_path: Path) -> None:
    claims = make_estate(tmp_path)

    result = runner.invoke(
        app, ["wave", "unreserve", "--session", "S", "T1", "-C", str(claims.root)]
    )

    assert result.exit_code == 0, result.output
    assert reservations(claims) == {}
