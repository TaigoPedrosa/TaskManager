import json
from pathlib import Path
from typing import Any

import pytest
from lifecycle_estate import add, make_estate

from taskmanager.core.enums import NodeKind
from taskmanager.core.models import FileLock, Lease, LeaseAction, Node
from taskmanager.core.status import SET_ASIDE, Action, DecisionStatus, Merge, Outcome, Status
from taskmanager.engine.claims import Claims
from taskmanager.engine.heuristics import score_every_task
from taskmanager.engine.snapshot import DisplayView, waits_on
from taskmanager.web.rows import (
    build_rows,
    canonical,
    counts_as_work,
    decisions_open,
    row_digest,
    statuses,
    statuses_hash,
)

FIXTURE = json.loads((Path(__file__).parent.parent / "fixtures" / "statuses_hash.json").read_text())
COUNTS_AS_WORK = json.loads(
    (Path(__file__).parent.parent / "fixtures" / "visibility_cases.json").read_text()
)["counts_as_work"]

_ROW_FIELDS = {
    "id",
    "kind",
    "title",
    "ordinal",
    "priority",
    "parent",
    "status",
    "display",
    "phase",
    "score",
    "target_repo",
    "acceptable_models",
    "review",
    "fix",
    "merge",
    "outcome",
    "requires",
    "lease",
    "waits_on",
    "superseded_by",
    "child_count",
    "rev",
    "archived",
}


def test_canonical_of_the_fixtures_statuses_matches_its_golden_string() -> None:
    assert canonical(FIXTURE["statuses"]) == FIXTURE["canonical"]


def test_statuses_hash_of_the_fixtures_statuses_matches_its_golden_hash() -> None:
    assert statuses_hash(FIXTURE["statuses"]) == FIXTURE["hash"]


def test_a_row_holds_exactly_the_contracts_fields(tmp_path: Path) -> None:
    claims = make_estate(tmp_path)
    add(claims, "S1", NodeKind.SPEC)
    add(claims, "P1", NodeKind.PLAN, parent="S1")
    add(claims, "T1", NodeKind.TASK, parent="P1")

    rows = build_rows(DisplayView(claims.snapshots))

    assert set(rows["T1"]) == _ROW_FIELDS
    assert set(rows["P1"]) == _ROW_FIELDS
    assert set(rows["S1"]) == _ROW_FIELDS


def test_decisions_never_become_rows(tmp_path: Path) -> None:
    claims = make_estate(tmp_path)
    add(claims, "T1", NodeKind.TASK)
    claims.nodes.save_node(Node(id="D1", kind=NodeKind.DECISION, title="D1"))

    rows = build_rows(DisplayView(claims.snapshots))

    assert set(rows) == {"T1"}


def test_score_is_null_off_tasks_and_the_shared_formula_on_a_task(tmp_path: Path) -> None:
    claims = make_estate(tmp_path)
    add(claims, "S1", NodeKind.SPEC)
    add(claims, "P1", NodeKind.PLAN, parent="S1")
    add(claims, "T1", NodeKind.TASK, parent="P1")

    view = DisplayView(claims.snapshots)
    rows = build_rows(view)
    expected = score_every_task(view.snapshot)["T1"]

    assert rows["S1"]["score"] is None
    assert rows["P1"]["score"] is None
    assert rows["T1"]["score"] == expected


def test_a_rows_parent_and_child_count_follow_contains(tmp_path: Path) -> None:
    claims = make_estate(tmp_path)
    add(claims, "S1", NodeKind.SPEC)
    add(claims, "P1", NodeKind.PLAN, parent="S1")
    add(claims, "T1", NodeKind.TASK, parent="P1")
    add(claims, "T2", NodeKind.TASK, parent="P1")

    rows = build_rows(DisplayView(claims.snapshots))

    assert rows["S1"]["parent"] is None
    assert rows["P1"]["parent"] == "S1"
    assert rows["T1"]["parent"] == "P1"
    assert rows["S1"]["child_count"] == 1
    assert rows["P1"]["child_count"] == 2
    assert rows["T1"]["child_count"] == 0


def test_a_rows_lease_holds_only_agent_and_action(tmp_path: Path) -> None:
    claims = make_estate(tmp_path)
    add(claims, "T1", NodeKind.TASK)
    claims.runtime.acquire_lease(
        Lease(
            task_id="T1",
            agent_id="agent-1",
            session_id="s1",
            branch_name="tm/T1",
            action=Action.IMPLEMENT,
        ),
        [FileLock(file_path="f", task_id="T1")],
    )

    rows = build_rows(DisplayView(claims.snapshots))

    assert rows["T1"]["lease"] == {"agent_id": "agent-1", "action": "implement"}


def test_a_rows_lease_is_null_with_no_lease(tmp_path: Path) -> None:
    claims = make_estate(tmp_path)
    add(claims, "T1", NodeKind.TASK)

    rows = build_rows(DisplayView(claims.snapshots))

    assert rows["T1"]["lease"] is None


def test_stored_fields_pass_through_as_stored(tmp_path: Path) -> None:
    claims = make_estate(tmp_path)
    add(
        claims,
        "T1",
        NodeKind.TASK,
        priority=77,
        ordinal=3,
        merge=Merge.PARENT,
        models=["claude-sonnet-5"],
        requires=["some-file.py"],
        status=Status.REVIEWED,
        outcome=Outcome.APPROVE,
    )

    row = build_rows(DisplayView(claims.snapshots))["T1"]

    assert row["priority"] == 77
    assert row["ordinal"] == 3
    assert row["merge"] == "parent"
    assert row["acceptable_models"] == ["claude-sonnet-5"]
    assert row["requires"] == ["some-file.py"]
    assert row["status"] == "REVIEWED"
    assert row["outcome"] == "approve"


def test_a_rows_waits_on_is_waits_ons_work_then_its_open_decisions(tmp_path: Path) -> None:
    claims = make_estate(tmp_path)
    add(claims, "T1", NodeKind.TASK)
    add(claims, "T2", NodeKind.TASK, depends=("T1",))

    view = DisplayView(claims.snapshots)
    node = claims.nodes.get_node("T2")
    assert node is not None
    work, decisions = waits_on(view.snapshot, node)

    row = build_rows(view)["T2"]

    assert work == ["T1"]
    assert row["waits_on"] == [*work, *decisions]


def test_a_rows_digest_changes_exactly_when_a_field_does(tmp_path: Path) -> None:
    claims = make_estate(tmp_path)
    add(claims, "T1", NodeKind.TASK, priority=40)
    add(claims, "T2", NodeKind.TASK, priority=40)

    rows = build_rows(DisplayView(claims.snapshots))
    same_shape = {**rows["T2"], "id": "T1", "title": "T1"}

    assert row_digest(rows["T1"]) == row_digest(same_shape)
    assert row_digest(rows["T1"]) != row_digest({**rows["T1"], "priority": 41})


def test_decisions_open_counts_only_open_decisions(tmp_path: Path) -> None:
    claims = make_estate(tmp_path)
    claims.nodes.save_node(Node(id="D1", kind=NodeKind.DECISION, title="D1"))
    claims.nodes.save_node(
        Node(id="D2", kind=NodeKind.DECISION, title="D2", status=DecisionStatus.ANSWERED)
    )
    claims.nodes.save_node(Node(id="D3", kind=NodeKind.DECISION, title="D3"))

    assert decisions_open(DisplayView(claims.snapshots)) == 2


def test_statuses_counts_each_task_once_under_its_nearest_spec_and_plan(tmp_path: Path) -> None:
    claims = make_estate(tmp_path)
    # spec with a plan and two tasks
    add(claims, "S1", NodeKind.SPEC)
    add(claims, "P1", NodeKind.PLAN, parent="S1")
    add(claims, "T1", NodeKind.TASK, parent="P1", status=Status.READY)
    add(claims, "T2", NodeKind.TASK, parent="P1", status=Status.COMPLETED)
    # a spec with no plans at all
    add(claims, "S2", NodeKind.SPEC)
    # a spec-less plan with one task
    add(claims, "P2", NodeKind.PLAN)
    add(claims, "T3", NodeKind.TASK, parent="P2", status=Status.READY)
    # a task directly under a spec, no plan in between
    add(claims, "T4", NodeKind.TASK, parent="S1", status=Status.READY)
    # a fully orphan task
    add(claims, "T5", NodeKind.TASK, status=Status.READY)

    view = DisplayView(claims.snapshots)
    rows = build_rows(view)
    result = statuses(rows)

    def entry(spec: str | None, plan: str | None) -> dict[str, int]:
        [spec_entry] = [e for e in result if e["spec"] == spec]
        [plan_entry] = [p for p in spec_entry["plans"] if p["plan"] == plan]
        counts: dict[str, int] = plan_entry["counts"]
        return counts

    assert entry("S1", "P1") == {"READY": 1, "COMPLETED": 1}
    assert entry("S1", None) == {"READY": 1}
    assert entry(None, "P2") == {"READY": 1}
    assert entry(None, None) == {"READY": 1}
    [s2_entry] = [e for e in result if e["spec"] == "S2"]
    assert s2_entry["plans"] == []
    # specs sorted by id, null first
    assert [e["spec"] for e in result] == [None, "S1", "S2"]
    # plans sorted by id, null first
    [s1_entry] = [e for e in result if e["spec"] == "S1"]
    assert [p["plan"] for p in s1_entry["plans"]] == [None, "P1"]


def _lease(claims: Claims, node_id: str, action: LeaseAction) -> None:
    claims.runtime.acquire_lease(
        Lease(
            task_id=node_id,
            agent_id="agent-1",
            session_id="s1",
            branch_name=f"tm/{node_id}",
            action=action,
        ),
        [],
    )


def test_a_plan_past_ready_adds_its_own_unit_to_its_own_group(tmp_path: Path) -> None:
    claims = make_estate(tmp_path)
    add(claims, "S1", NodeKind.SPEC)
    add(
        claims,
        "P1",
        NodeKind.PLAN,
        parent="S1",
        status=Status.REVIEWING,
        claimed_from=Status.IMPLEMENTED,
    )
    add(claims, "T1", NodeKind.TASK, parent="P1", status=Status.COMPLETED)
    add(claims, "T2", NodeKind.TASK, parent="P1", status=Status.COMPLETED)
    _lease(claims, "P1", Action.REVIEW)

    rows = build_rows(DisplayView(claims.snapshots))
    [plan_entry] = [p for e in statuses(rows) if e["spec"] == "S1" for p in e["plans"]]

    assert rows["P1"]["display"] == "REVIEWING"
    assert plan_entry["counts"] == {"COMPLETED": 2, "REVIEWING": 1}


def test_a_completed_plan_adds_its_own_unit_as_one_more_completed(tmp_path: Path) -> None:
    claims = make_estate(tmp_path)
    add(claims, "S1", NodeKind.SPEC)
    add(claims, "P1", NodeKind.PLAN, parent="S1", status=Status.COMPLETED)
    add(claims, "T1", NodeKind.TASK, parent="P1", status=Status.COMPLETED)
    add(claims, "T2", NodeKind.TASK, parent="P1", status=Status.COMPLETED)

    rows = build_rows(DisplayView(claims.snapshots))
    [plan_entry] = [p for e in statuses(rows) if e["spec"] == "S1" for p in e["plans"]]

    assert plan_entry["counts"] == {"COMPLETED": 3}


def test_a_ready_plan_with_children_in_progress_adds_no_unit_of_its_own(tmp_path: Path) -> None:
    claims = make_estate(tmp_path)
    add(claims, "S1", NodeKind.SPEC)
    add(claims, "P1", NodeKind.PLAN, parent="S1", status=Status.READY)
    # started (not READY, not a set-aside exit) rolls the plan's own display up to IMPLEMENTING,
    # but its stored status stays READY, so it adds no unit of its own
    add(claims, "T1", NodeKind.TASK, parent="P1", status=Status.IMPLEMENTED)

    rows = build_rows(DisplayView(claims.snapshots))
    [plan_entry] = [p for e in statuses(rows) if e["spec"] == "S1" for p in e["plans"]]

    assert rows["P1"]["status"] == "READY"
    assert rows["P1"]["display"] == "IMPLEMENTING"
    assert plan_entry["counts"] == {rows["T1"]["display"]: 1}


def test_a_spec_past_ready_adds_its_own_unit_to_its_no_plan_group(tmp_path: Path) -> None:
    claims = make_estate(tmp_path)
    add(claims, "S1", NodeKind.SPEC, status=Status.FIXING, claimed_from=Status.REVIEWED)
    add(claims, "T1", NodeKind.TASK, parent="S1", status=Status.READY)
    _lease(claims, "S1", Action.FIX)

    rows = build_rows(DisplayView(claims.snapshots))
    [entry] = [e for e in statuses(rows) if e["spec"] == "S1"]
    [no_plan] = [p["counts"] for p in entry["plans"] if p["plan"] is None]

    assert rows["S1"]["display"] == "FIXING"
    assert no_plan == {"READY": 1, "FIXING": 1}


def test_a_deferred_plan_with_children_adds_no_unit_of_its_own(tmp_path: Path) -> None:
    claims = make_estate(tmp_path)
    add(claims, "S1", NodeKind.SPEC)
    add(claims, "P1", NodeKind.PLAN, parent="S1", status=Status.DEFERRED)
    add(claims, "T1", NodeKind.TASK, parent="P1", status=Status.DEFERRED)

    rows = build_rows(DisplayView(claims.snapshots))
    [plan_entry] = [p for e in statuses(rows) if e["spec"] == "S1" for p in e["plans"]]

    assert plan_entry["counts"] == {"DEFERRED": 1}


def test_a_childless_plan_past_ready_adds_no_unit(tmp_path: Path) -> None:
    claims = make_estate(tmp_path)
    add(claims, "S1", NodeKind.SPEC)
    add(claims, "P1", NodeKind.PLAN, parent="S1", status=Status.COMPLETED)

    rows = build_rows(DisplayView(claims.snapshots))
    [plan_entry] = [p for e in statuses(rows) if e["spec"] == "S1" for p in e["plans"]]

    assert plan_entry["counts"] == {}


@pytest.mark.parametrize(
    "case", COUNTS_AS_WORK, ids=lambda case: "-".join(map(str, case["row"].values()))
)
def test_counts_as_work_holds_for_a_task_or_a_started_container_with_children(
    case: dict[str, Any],
) -> None:
    assert counts_as_work(case["row"]) is case["counts"]


def test_counts_as_work_cases_name_every_status_a_started_container_never_counts_under() -> None:
    # store.js hard-codes this set and runs the same cases, so a status added to SET_ASIDE alone
    # has to reach the cases, and through them the JS mirror
    never = {
        case["row"]["status"]
        for case in COUNTS_AS_WORK
        if case["row"]["kind"] != NodeKind.TASK.value
        and case["row"]["child_count"]
        and not case["counts"]
    }

    assert never == {Status.READY.value, *(status.value for status in SET_ASIDE)}


def test_building_rows_and_statuses_reads_only_the_views_own_bulk_read(
    tmp_path: Path,
) -> None:
    claims = make_estate(tmp_path)
    add(claims, "S1", NodeKind.SPEC)
    add(claims, "P1", NodeKind.PLAN, parent="S1")
    add(claims, "T1", NodeKind.TASK, parent="P1")
    add(claims, "T2", NodeKind.TASK, parent="P1", depends=("T1",))

    view = DisplayView(claims.snapshots)

    statements: list[str] = []
    with claims.nodes.db.get_state_connection() as conn:
        conn.set_trace_callback(lambda sql: statements.append(sql))
    try:
        rows = build_rows(view)
        statuses(rows)
        decisions_open(view)
    finally:
        with claims.nodes.db.get_state_connection() as conn:
            conn.set_trace_callback(None)

    assert statements == []
