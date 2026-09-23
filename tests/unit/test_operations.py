from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from taskmanager.core.enums import NodeKind, NodeStatus, VerificationType
from taskmanager.core.models import Lease
from taskmanager.db.connection import DatabaseManager
from taskmanager.db.ledger_repo import LedgerRepository
from taskmanager.db.node_repo import NodeRepository
from taskmanager.db.runtime_repo import RuntimeRepository
from taskmanager.engine.decisions import read_decision
from taskmanager.engine.graph import GraphEngine
from taskmanager.engine.operations import OperationError, Operations
from taskmanager.engine.runtime import ExecutionCoordinator
from taskmanager.engine.verification import VerificationEngine


@pytest.fixture
def ops_setup(
    tmp_path: Path,
) -> tuple[NodeRepository, RuntimeRepository, LedgerRepository, Operations]:
    db = DatabaseManager(tmp_path / "db")
    db.init_all()
    node_repo = NodeRepository(db)
    runtime_repo = RuntimeRepository(db)
    ledger_repo = LedgerRepository(db)
    graph = GraphEngine(node_repo=node_repo, runtime_repo=runtime_repo)
    coordinator = ExecutionCoordinator(node_repo, runtime_repo, graph, git_mgr=None)
    verification_engine = VerificationEngine(tmp_path)
    ops = Operations(
        node_repo,
        runtime_repo,
        graph,
        coordinator,
        ledger_repo,
        verification_engine,
        actor="tester",
    )
    return node_repo, runtime_repo, ledger_repo, ops


def _seed_task(ops: Operations) -> tuple[str, str, str]:
    spec_id = ops.add_spec("Spec", slug="S1")
    plan_id, _ = ops.add_plan("Plan", spec_id, slug="P1")
    task_id = ops.add_task("Task", plan_id, slug="T1")
    return spec_id, plan_id, task_id


def _last_event_actor(ledger_repo: LedgerRepository) -> str:
    events = ledger_repo.list_events(limit=1)
    assert events
    return events[0].actor_id


# -- spec / plan / task creation ---------------------------------------------------------


def test_add_spec_creates_node_and_ledger_event(ops_setup: tuple) -> None:
    node_repo, _runtime_repo, ledger_repo, ops = ops_setup
    spec_id = ops.add_spec("My Spec", slug="MYSPEC", priority=70)
    node = node_repo.get_node(spec_id)
    assert node is not None
    assert node.title == "My Spec"
    assert node.priority == 70
    assert _last_event_actor(ledger_repo) == "tester"


def test_add_plan_with_review_gate_injects_gate_and_records_it(ops_setup: tuple) -> None:
    node_repo, _runtime_repo, _ledger_repo, ops = ops_setup
    spec_id = ops.add_spec("S", slug="S1")
    plan_id, gate_id = ops.add_plan("P", spec_id, slug="P1", require_review=True)
    assert gate_id == f"{plan_id}-REV"
    assert node_repo.get_node(gate_id) is not None
    assert plan_id in node_repo.get_children(spec_id)


def test_add_task_wires_deps_and_parent(ops_setup: tuple) -> None:
    node_repo, _runtime_repo, _ledger_repo, ops = ops_setup
    _spec_id, plan_id, first = _seed_task(ops)
    second = ops.add_task("Second", plan_id, slug="T2", depends_on=[first])
    assert second in node_repo.get_children(plan_id)
    assert node_repo.get_dependencies(second) == [first]


# -- update_node ---------------------------------------------------------------------------


def test_update_node_changes_only_given_fields(ops_setup: tuple) -> None:
    node_repo, _runtime_repo, ledger_repo, ops = ops_setup
    _spec_id, _plan_id, task_id = _seed_task(ops)
    changed = ops.update_node(task_id, priority=90, repo="web")
    assert changed == {"priority": 90, "target_repo": "web"}
    node = node_repo.get_node(task_id)
    assert node is not None
    assert node.priority == 90
    assert node.target_repo == "web"
    assert node.title == "Task"
    assert _last_event_actor(ledger_repo) == "tester"


def test_update_node_frontmatter_set_and_unset(ops_setup: tuple) -> None:
    node_repo, _runtime_repo, _ledger_repo, ops = ops_setup
    _spec_id, _plan_id, task_id = _seed_task(ops)
    ops.update_node(task_id, frontmatter_set={"declared_files": ["a", "b"]})
    node = node_repo.get_node(task_id)
    assert node is not None
    assert node.frontmatter == {"declared_files": ["a", "b"]}
    ops.update_node(task_id, frontmatter_unset=["declared_files"])
    node = node_repo.get_node(task_id)
    assert node is not None
    assert node.frontmatter == {}


def test_update_node_missing_task_refuses_and_writes_nothing(ops_setup: tuple) -> None:
    _node_repo, _runtime_repo, ledger_repo, ops = ops_setup
    before = len(ledger_repo.list_events(limit=1000))
    with pytest.raises(OperationError) as exc:
        ops.update_node("NOPE", title="x")
    assert exc.value.status_code == 404
    assert len(ledger_repo.list_events(limit=1000)) == before


def test_update_node_bad_priority_refuses_and_writes_nothing(ops_setup: tuple) -> None:
    node_repo, _runtime_repo, ledger_repo, ops = ops_setup
    _spec_id, _plan_id, task_id = _seed_task(ops)
    before = len(ledger_repo.list_events(limit=1000))
    with pytest.raises(OperationError):
        ops.update_node(task_id, priority=101)
    assert node_repo.get_node(task_id).priority == 50
    assert len(ledger_repo.list_events(limit=1000)) == before


def test_update_node_nothing_to_update_refuses(ops_setup: tuple) -> None:
    _node_repo, _runtime_repo, ledger_repo, ops = ops_setup
    _spec_id, _plan_id, task_id = _seed_task(ops)
    before = len(ledger_repo.list_events(limit=1000))
    with pytest.raises(OperationError) as exc:
        ops.update_node(task_id)
    assert exc.value.status_code == 400
    assert len(ledger_repo.list_events(limit=1000)) == before


# -- set_dependencies ------------------------------------------------------------------------


def test_set_dependencies_adds_and_removes_with_gate(ops_setup: tuple) -> None:
    node_repo, _runtime_repo, ledger_repo, ops = ops_setup
    _spec_id, plan_id, first = _seed_task(ops)
    second = ops.add_task("Second", plan_id, slug="T2")
    edges = ops.set_dependencies(second, [(first, NodeStatus.WAITING_REVIEW)], [])
    assert edges == [(first, NodeStatus.WAITING_REVIEW)]
    assert _last_event_actor(ledger_repo) == "tester"
    edges = ops.set_dependencies(second, [], [first])
    assert edges == []
    assert node_repo.get_dependencies(second) == []


def test_set_dependencies_missing_task_refuses(ops_setup: tuple) -> None:
    _node_repo, _runtime_repo, _ledger_repo, ops = ops_setup
    with pytest.raises(OperationError) as exc:
        ops.set_dependencies("NOPE", [], ["also-nope"])
    assert exc.value.status_code == 404


def test_set_dependencies_cycle_refuses_and_writes_nothing(ops_setup: tuple) -> None:
    node_repo, _runtime_repo, ledger_repo, ops = ops_setup
    _spec_id, plan_id, first = _seed_task(ops)
    second = ops.add_task("Second", plan_id, slug="T2", depends_on=[first])
    before = len(ledger_repo.list_events(limit=1000))
    with pytest.raises(OperationError) as exc:
        ops.set_dependencies(first, [(second, None)], [])
    assert exc.value.status_code == 409
    assert node_repo.get_dependencies(first) == []
    assert len(ledger_repo.list_events(limit=1000)) == before


# -- supersede --------------------------------------------------------------------------------


def test_supersede_marks_old_superseded_and_transfers_blocks(ops_setup: tuple) -> None:
    node_repo, _runtime_repo, ledger_repo, ops = ops_setup
    _spec_id, plan_id, old = _seed_task(ops)
    new = ops.add_task("New", plan_id, slug="NEW")
    blocked = ops.add_task("Blocked", plan_id, slug="BLK", depends_on=[old])
    ops.supersede(old, new)
    assert node_repo.get_node(old).status == NodeStatus.SUPERSEDED
    assert node_repo.get_dependencies(blocked) == [new]
    assert _last_event_actor(ledger_repo) == "tester"


def test_supersede_missing_old_refuses(ops_setup: tuple) -> None:
    _node_repo, _runtime_repo, _ledger_repo, ops = ops_setup
    with pytest.raises(OperationError) as exc:
        ops.supersede("NOPE", "ALSO-NOPE")
    assert exc.value.status_code == 404


def test_supersede_missing_new_refuses_and_writes_nothing(ops_setup: tuple) -> None:
    node_repo, _runtime_repo, ledger_repo, ops = ops_setup
    _spec_id, _plan_id, old = _seed_task(ops)
    before = len(ledger_repo.list_events(limit=1000))
    with pytest.raises(OperationError) as exc:
        ops.supersede(old, "NOPE")
    assert exc.value.status_code == 400
    assert node_repo.get_node(old).status == NodeStatus.NOT_STARTED
    assert len(ledger_repo.list_events(limit=1000)) == before


# -- move_task --------------------------------------------------------------------------------


def test_move_task_reparents(ops_setup: tuple) -> None:
    node_repo, _runtime_repo, ledger_repo, ops = ops_setup
    spec_id, plan_id, task_id = _seed_task(ops)
    other_plan, _ = ops.add_plan("Other", spec_id, slug="P2")
    ops.move_task(task_id, other_plan)
    assert task_id not in node_repo.get_children(plan_id)
    assert task_id in node_repo.get_children(other_plan)
    assert _last_event_actor(ledger_repo) == "tester"


def test_move_task_missing_task_refuses(ops_setup: tuple) -> None:
    _node_repo, _runtime_repo, _ledger_repo, ops = ops_setup
    _spec_id, plan_id, _task_id = _seed_task(ops)
    with pytest.raises(OperationError) as exc:
        ops.move_task("NOPE", plan_id)
    assert exc.value.status_code == 404


def test_move_task_missing_plan_refuses_and_writes_nothing(ops_setup: tuple) -> None:
    node_repo, _runtime_repo, ledger_repo, ops = ops_setup
    _spec_id, plan_id, task_id = _seed_task(ops)
    before = len(ledger_repo.list_events(limit=1000))
    with pytest.raises(OperationError) as exc:
        ops.move_task(task_id, "NOPE")
    assert exc.value.status_code == 404
    assert task_id in node_repo.get_children(plan_id)
    assert len(ledger_repo.list_events(limit=1000)) == before


# -- status / leases ----------------------------------------------------------------------


def test_set_status_stops_task_and_releases_lease(ops_setup: tuple) -> None:
    node_repo, runtime_repo, ledger_repo, ops = ops_setup
    _spec_id, _plan_id, task_id = _seed_task(ops)
    runtime_repo.acquire_lease(
        Lease(task_id=task_id, agent_id="a", session_id="s", branch_name=f"tm/{task_id}"), []
    )
    ops.set_status(task_id, NodeStatus.COMPLETED)
    assert node_repo.get_node(task_id).status == NodeStatus.COMPLETED
    assert runtime_repo.get_lease(task_id) is None
    assert _last_event_actor(ledger_repo) == "tester"


def test_release_lease_drops_lease_without_changing_status(ops_setup: tuple) -> None:
    node_repo, runtime_repo, ledger_repo, ops = ops_setup
    _spec_id, _plan_id, task_id = _seed_task(ops)
    runtime_repo.acquire_lease(
        Lease(task_id=task_id, agent_id="a", session_id="s", branch_name=f"tm/{task_id}"), []
    )
    ops.release_lease(task_id)
    assert runtime_repo.get_lease(task_id) is None
    assert node_repo.get_node(task_id).status == NodeStatus.NOT_STARTED
    assert _last_event_actor(ledger_repo) == "tester"


def test_sweep_leases_rolls_back_status_of_expired_claims(ops_setup: tuple) -> None:
    node_repo, runtime_repo, ledger_repo, ops = ops_setup
    _spec_id, _plan_id, task_id = _seed_task(ops)
    node = node_repo.get_node(task_id)
    node.status = NodeStatus.IMPLEMENTING
    node_repo.save_node(node)
    stale = datetime.now(tz=UTC) - timedelta(hours=1)
    runtime_repo.acquire_lease(
        Lease(
            task_id=task_id,
            agent_id="a",
            session_id="s",
            branch_name=f"tm/{task_id}",
            last_heartbeat=stale,
            ttl_seconds=60,
        ),
        [],
    )
    swept = ops.sweep_leases()
    assert swept == [task_id]
    assert node_repo.get_node(task_id).status == NodeStatus.NOT_STARTED
    assert _last_event_actor(ledger_repo) == "tester"


def test_sweep_leases_with_nothing_expired_writes_no_ledger(ops_setup: tuple) -> None:
    _node_repo, _runtime_repo, ledger_repo, ops = ops_setup
    before = len(ledger_repo.list_events(limit=1000))
    assert ops.sweep_leases() == []
    assert len(ledger_repo.list_events(limit=1000)) == before


# -- sections -----------------------------------------------------------------------------


def test_set_section_creates_then_updates_in_place(ops_setup: tuple) -> None:
    node_repo, _runtime_repo, ledger_repo, ops = ops_setup
    _spec_id, _plan_id, task_id = _seed_task(ops)
    ops.set_section(task_id, "steps", "first")
    ops.set_section(task_id, "steps", "second")
    secs = node_repo.get_all_sections(task_id)
    assert [s.content for s in secs] == ["second"]
    assert _last_event_actor(ledger_repo) == "tester"


def test_set_section_missing_node_refuses(ops_setup: tuple) -> None:
    _node_repo, _runtime_repo, ledger_repo, ops = ops_setup
    before = len(ledger_repo.list_events(limit=1000))
    with pytest.raises(OperationError) as exc:
        ops.set_section("NOPE", "steps", "x")
    assert exc.value.status_code == 404
    assert len(ledger_repo.list_events(limit=1000)) == before


def test_remove_section_deletes_it(ops_setup: tuple) -> None:
    node_repo, _runtime_repo, ledger_repo, ops = ops_setup
    _spec_id, _plan_id, task_id = _seed_task(ops)
    ops.set_section(task_id, "steps", "content")
    ops.remove_section(task_id, "steps")
    assert node_repo.get_section(task_id, "steps") is None
    assert _last_event_actor(ledger_repo) == "tester"


def test_remove_section_missing_refuses_and_writes_nothing(ops_setup: tuple) -> None:
    _node_repo, _runtime_repo, ledger_repo, ops = ops_setup
    _spec_id, _plan_id, task_id = _seed_task(ops)
    before = len(ledger_repo.list_events(limit=1000))
    with pytest.raises(OperationError) as exc:
        ops.remove_section(task_id, "nope")
    assert exc.value.status_code == 404
    assert len(ledger_repo.list_events(limit=1000)) == before


# -- verifications --------------------------------------------------------------------------


def test_add_and_remove_verification(ops_setup: tuple) -> None:
    node_repo, _runtime_repo, ledger_repo, ops = ops_setup
    _spec_id, _plan_id, task_id = _seed_task(ops)
    ver = ops.add_verification(task_id, VerificationType.FILE_EXISTS, "out.txt")
    assert ver.id is not None
    assert len(node_repo.get_verifications(task_id)) == 1
    assert _last_event_actor(ledger_repo) == "tester"
    ops.remove_verification(task_id, ver.id)
    assert node_repo.get_verifications(task_id) == []


def test_remove_verification_missing_refuses_and_writes_nothing(ops_setup: tuple) -> None:
    _node_repo, _runtime_repo, ledger_repo, ops = ops_setup
    _spec_id, _plan_id, task_id = _seed_task(ops)
    before = len(ledger_repo.list_events(limit=1000))
    with pytest.raises(OperationError) as exc:
        ops.remove_verification(task_id, 999)
    assert exc.value.status_code == 404
    assert len(ledger_repo.list_events(limit=1000)) == before


def test_run_verifications_reports_results_and_records_ledger(
    ops_setup: tuple, tmp_path: Path
) -> None:
    _node_repo, _runtime_repo, ledger_repo, ops = ops_setup
    _spec_id, _plan_id, task_id = _seed_task(ops)
    (tmp_path / "out.txt").write_text("ok", encoding="utf-8")
    ops.add_verification(task_id, VerificationType.FILE_EXISTS, "out.txt")
    all_passed, results = ops.run_verifications(task_id)
    assert all_passed is True
    assert len(results) == 1
    assert _last_event_actor(ledger_repo) == "tester"


def test_run_verifications_empty_set_refuses(ops_setup: tuple) -> None:
    _node_repo, _runtime_repo, ledger_repo, ops = ops_setup
    _spec_id, _plan_id, task_id = _seed_task(ops)
    before = len(ledger_repo.list_events(limit=1000))
    with pytest.raises(OperationError) as exc:
        ops.run_verifications(task_id)
    assert exc.value.status_code == 400
    assert len(ledger_repo.list_events(limit=1000)) == before


# -- actor ----------------------------------------------------------------------------------


def test_with_actor_returns_a_new_operations_carrying_the_actor(ops_setup: tuple) -> None:
    _node_repo, _runtime_repo, ledger_repo, ops = ops_setup
    web_ops = ops.with_actor("web")
    assert web_ops is not ops
    web_ops.add_spec("S", slug="WEBS")
    assert _last_event_actor(ledger_repo) == "web"


# -- set_status with an atomic section -------------------------------------------------------


def test_set_status_with_section_writes_status_and_section_together(ops_setup: tuple) -> None:
    node_repo, _runtime_repo, ledger_repo, ops = ops_setup
    _spec_id, _plan_id, task_id = _seed_task(ops)
    ops.set_status(
        task_id, NodeStatus.COMPLETED, section=("ruling", "the ruling text", "## Ruling")
    )
    assert node_repo.get_node(task_id).status == NodeStatus.COMPLETED
    sec = node_repo.get_section(task_id, "ruling")
    assert sec is not None
    assert sec.content == "the ruling text"
    assert _last_event_actor(ledger_repo) == "tester"


def test_set_status_failure_leaves_the_section_unwritten(ops_setup: tuple) -> None:
    node_repo, _runtime_repo, _ledger_repo, ops = ops_setup
    _spec_id, _plan_id, task_id = _seed_task(ops)

    def boom(**_kwargs: object) -> None:
        raise ValueError("stop_task refused")

    ops.coordinator.stop_task = boom  # type: ignore[method-assign]
    with pytest.raises(ValueError):
        ops.set_status(task_id, NodeStatus.COMPLETED, section=("ruling", "text", None))
    assert node_repo.get_section(task_id, "ruling") is None


# -- decisions --------------------------------------------------------------------------------


def test_add_decision_creates_node_with_options_and_context(ops_setup: tuple) -> None:
    node_repo, _runtime_repo, ledger_repo, ops = ops_setup
    decision_id = ops.add_decision(
        "Which auth flow?",
        slug="auth-flow",
        context="some context",
        options=["a|Option A|first", "b|Option B|second"],
        recommend="a",
    )
    assert decision_id == "decision-auth-flow"
    node = node_repo.get_node(decision_id)
    assert node is not None
    assert node.kind == NodeKind.DECISION
    assert node.status == NodeStatus.NOT_STARTED
    data = read_decision(node)
    assert [o.key for o in data.options] == ["a", "b"]
    assert data.options[0].recommended is True
    assert data.options[1].recommended is False
    assert node_repo.get_section(decision_id, "context").content == "some context"
    assert _last_event_actor(ledger_repo) == "tester"


def test_add_decision_without_slug_numbers_itself(ops_setup: tuple) -> None:
    _node_repo, _runtime_repo, _ledger_repo, ops = ops_setup
    first = ops.add_decision("Q1")
    second = ops.add_decision("Q2")
    assert first == "decision-D1"
    assert second == "decision-D2"


def test_add_decision_bad_recommend_refuses_and_writes_nothing(ops_setup: tuple) -> None:
    node_repo, _runtime_repo, ledger_repo, ops = ops_setup
    before = len(ledger_repo.list_events(limit=1000))
    with pytest.raises(OperationError) as exc:
        ops.add_decision("Q", slug="q1", options=["a|A"], recommend="nope")
    assert exc.value.status_code == 400
    assert node_repo.get_node("decision-q1") is None
    assert len(ledger_repo.list_events(limit=1000)) == before


def test_add_decision_blocks_wires_depends_on_from_each_task(ops_setup: tuple) -> None:
    node_repo, _runtime_repo, _ledger_repo, ops = ops_setup
    _spec_id, _plan_id, task_id = _seed_task(ops)
    decision_id = ops.add_decision("Q", slug="q1", blocks=[task_id])
    assert decision_id in node_repo.get_dependencies(task_id)


def test_add_decision_unknown_blocked_task_refuses_and_writes_nothing(ops_setup: tuple) -> None:
    node_repo, _runtime_repo, ledger_repo, ops = ops_setup
    before = len(ledger_repo.list_events(limit=1000))
    with pytest.raises(OperationError) as exc:
        ops.add_decision("Q", slug="q1", blocks=["NOPE"])
    assert exc.value.status_code == 404
    assert node_repo.get_node("decision-q1") is None
    assert len(ledger_repo.list_events(limit=1000)) == before


def test_answer_decision_with_option_completes_it(ops_setup: tuple) -> None:
    node_repo, _runtime_repo, ledger_repo, ops = ops_setup
    decision_id = ops.add_decision("Q", slug="q1", options=["a|A", "b|B"])
    ops.answer_decision(decision_id, option="a", rationale="because", by="owner")
    node = node_repo.get_node(decision_id)
    assert node.status == NodeStatus.COMPLETED
    data = read_decision(node)
    assert data.answer is not None
    assert data.answer.option == "a"
    assert data.answer.rationale == "because"
    assert data.answer.answered_by == "owner"
    assert _last_event_actor(ledger_repo) == "tester"


def test_answer_decision_unknown_option_refuses(ops_setup: tuple) -> None:
    _node_repo, _runtime_repo, _ledger_repo, ops = ops_setup
    decision_id = ops.add_decision("Q", slug="q1", options=["a|A"])
    with pytest.raises(OperationError) as exc:
        ops.answer_decision(decision_id, option="nope")
    assert exc.value.status_code == 400


def test_answer_decision_custom_refused_when_not_allowed(ops_setup: tuple) -> None:
    _node_repo, _runtime_repo, _ledger_repo, ops = ops_setup
    decision_id = ops.add_decision("Q", slug="q1", options=["a|A"], allow_custom=False)
    with pytest.raises(OperationError) as exc:
        ops.answer_decision(decision_id, text="custom answer")
    assert exc.value.status_code == 400


def test_answer_decision_custom_answer_when_allowed(ops_setup: tuple) -> None:
    node_repo, _runtime_repo, _ledger_repo, ops = ops_setup
    decision_id = ops.add_decision("Q", slug="q1")
    ops.answer_decision(decision_id, text="do the custom thing")
    data = read_decision(node_repo.get_node(decision_id))
    assert data.answer.option is None
    assert data.answer.text == "do the custom thing"


def test_answer_decision_not_open_refuses(ops_setup: tuple) -> None:
    _node_repo, _runtime_repo, _ledger_repo, ops = ops_setup
    decision_id = ops.add_decision("Q", slug="q1", options=["a|A"])
    ops.answer_decision(decision_id, option="a")
    with pytest.raises(OperationError) as exc:
        ops.answer_decision(decision_id, option="a")
    assert exc.value.status_code == 409


def test_reopen_decision_clears_answer_and_returns_to_open(ops_setup: tuple) -> None:
    node_repo, _runtime_repo, ledger_repo, ops = ops_setup
    decision_id = ops.add_decision("Q", slug="q1", options=["a|A"])
    ops.answer_decision(decision_id, option="a")
    ops.reopen_decision(decision_id)
    node = node_repo.get_node(decision_id)
    assert node.status == NodeStatus.NOT_STARTED
    assert read_decision(node).answer is None
    assert _last_event_actor(ledger_repo) == "tester"


def test_withdraw_decision_sets_abandoned_with_reason(ops_setup: tuple) -> None:
    node_repo, _runtime_repo, ledger_repo, ops = ops_setup
    decision_id = ops.add_decision("Q", slug="q1")
    ops.withdraw_decision(decision_id, reason="no longer relevant")
    node = node_repo.get_node(decision_id)
    assert node.status == NodeStatus.ABANDONED
    assert read_decision(node).withdrawn_reason == "no longer relevant"
    assert _last_event_actor(ledger_repo) == "tester"


def test_link_decision_add_and_remove(ops_setup: tuple) -> None:
    node_repo, _runtime_repo, ledger_repo, ops = ops_setup
    _spec_id, _plan_id, task_id = _seed_task(ops)
    decision_id = ops.add_decision("Q", slug="q1")
    ops.link_decision(decision_id, add=[task_id])
    assert decision_id in node_repo.get_dependencies(task_id)
    ops.link_decision(decision_id, remove=[task_id])
    assert decision_id not in node_repo.get_dependencies(task_id)
    assert _last_event_actor(ledger_repo) == "tester"


def test_link_decision_remove_not_linked_refuses(ops_setup: tuple) -> None:
    _node_repo, _runtime_repo, _ledger_repo, ops = ops_setup
    _spec_id, _plan_id, task_id = _seed_task(ops)
    decision_id = ops.add_decision("Q", slug="q1")
    with pytest.raises(OperationError) as exc:
        ops.link_decision(decision_id, remove=[task_id])
    assert exc.value.status_code == 409


def test_get_decision_missing_refuses(ops_setup: tuple) -> None:
    _node_repo, _runtime_repo, _ledger_repo, ops = ops_setup
    with pytest.raises(OperationError) as exc:
        ops.answer_decision("NOPE", option="a")
    assert exc.value.status_code == 404


# -- attachments --------------------------------------------------------------------------------


def test_attach_copies_file_content_addressed_and_records_entry(
    ops_setup: tuple, tmp_path: Path
) -> None:
    node_repo, _runtime_repo, ledger_repo, ops = ops_setup
    _spec_id, _plan_id, task_id = _seed_task(ops)
    src = tmp_path / "shot.png"
    src.write_bytes(b"fake-png-bytes")
    entry = ops.attach(task_id, src, caption="a screenshot")
    node = node_repo.get_node(task_id)
    assert node.frontmatter["attachments"] == [entry]
    assert entry["caption"] == "a screenshot"
    assert entry["name"] == "shot.png"
    assert (ops._assets_dir() / entry["asset"]).read_bytes() == b"fake-png-bytes"
    assert _last_event_actor(ledger_repo) == "tester"


def test_attach_same_content_twice_is_idempotent_on_disk(ops_setup: tuple, tmp_path: Path) -> None:
    node_repo, _runtime_repo, _ledger_repo, ops = ops_setup
    _spec_id, _plan_id, task_id = _seed_task(ops)
    src = tmp_path / "shot.png"
    src.write_bytes(b"same-bytes")
    first = ops.attach(task_id, src)
    second = ops.attach(task_id, src)
    assert first["asset"] == second["asset"]
    assert len(node_repo.get_node(task_id).frontmatter["attachments"]) == 2


def test_attach_over_size_limit_refuses(
    ops_setup: tuple, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _node_repo, _runtime_repo, ledger_repo, ops = ops_setup
    _spec_id, _plan_id, task_id = _seed_task(ops)
    monkeypatch.setattr("taskmanager.engine.assets.MAX_ASSET_BYTES", 4)
    src = tmp_path / "big.bin"
    src.write_bytes(b"way too big")
    before = len(ledger_repo.list_events(limit=1000))
    with pytest.raises(OperationError) as exc:
        ops.attach(task_id, src)
    assert exc.value.status_code == 400
    assert len(ledger_repo.list_events(limit=1000)) == before


def test_attach_defaults_source_to_project_relative_path(ops_setup: tuple, tmp_path: Path) -> None:
    _node_repo, _runtime_repo, _ledger_repo, ops = ops_setup
    _spec_id, _plan_id, task_id = _seed_task(ops)
    src = tmp_path / "project-file.png"
    src.write_bytes(b"content")
    entry = ops.attach(task_id, src)
    assert entry["source"]["uri"] == "project-file.png"
    assert entry["source"]["sha256"] is not None
    assert entry["source"]["state"] == "fresh"


def test_attach_explicit_non_project_source_is_unverifiable(
    ops_setup: tuple, tmp_path: Path
) -> None:
    _node_repo, _runtime_repo, _ledger_repo, ops = ops_setup
    _spec_id, _plan_id, task_id = _seed_task(ops)
    src = tmp_path / "shot.png"
    src.write_bytes(b"content")
    entry = ops.attach(task_id, src, source="figma:abc:123")
    assert entry["source"]["uri"] == "figma:abc:123"
    assert entry["source"]["state"] == "unverifiable"
    assert entry["source"]["sha256"] is None


def test_detach_keeps_file_while_another_node_still_references_it(
    ops_setup: tuple, tmp_path: Path
) -> None:
    node_repo, _runtime_repo, ledger_repo, ops = ops_setup
    _spec_id, plan_id, task_id = _seed_task(ops)
    other_task = ops.add_task("Other", plan_id, slug="T2")
    src = tmp_path / "shared.png"
    src.write_bytes(b"shared-bytes")
    entry_a = ops.attach(task_id, src)
    ops.attach(other_task, src)
    ops.detach(task_id, entry_a["asset"])
    assert node_repo.get_node(task_id).frontmatter["attachments"] == []
    assert (ops._assets_dir() / entry_a["asset"]).exists()
    assert _last_event_actor(ledger_repo) == "tester"


def test_detach_removes_file_once_no_node_references_it(ops_setup: tuple, tmp_path: Path) -> None:
    _node_repo, _runtime_repo, _ledger_repo, ops = ops_setup
    _spec_id, _plan_id, task_id = _seed_task(ops)
    src = tmp_path / "solo.png"
    src.write_bytes(b"solo-bytes")
    entry = ops.attach(task_id, src)
    ops.detach(task_id, entry["asset"])
    assert not (ops._assets_dir() / entry["asset"]).exists()


def test_detach_missing_asset_refuses(ops_setup: tuple) -> None:
    _node_repo, _runtime_repo, ledger_repo, ops = ops_setup
    _spec_id, _plan_id, task_id = _seed_task(ops)
    before = len(ledger_repo.list_events(limit=1000))
    with pytest.raises(OperationError) as exc:
        ops.detach(task_id, "nope.png")
    assert exc.value.status_code == 404
    assert len(ledger_repo.list_events(limit=1000)) == before


def test_attach_replace_recaptures_keeping_caption_and_source(
    ops_setup: tuple, tmp_path: Path
) -> None:
    node_repo, _runtime_repo, _ledger_repo, ops = ops_setup
    _spec_id, _plan_id, task_id = _seed_task(ops)
    original = tmp_path / "v1.png"
    original.write_bytes(b"v1-bytes")
    first = ops.attach(task_id, original, caption="a screenshot")

    updated = tmp_path / "v2.png"
    updated.write_bytes(b"v2-bytes-different")
    second = ops.attach(task_id, updated, replace=first["asset"])

    attachments = node_repo.get_node(task_id).frontmatter["attachments"]
    assert len(attachments) == 1
    assert attachments[0]["asset"] == second["asset"]
    assert attachments[0]["asset"] != first["asset"]
    assert attachments[0]["caption"] == "a screenshot"


def test_attach_replace_unknown_asset_refuses(ops_setup: tuple, tmp_path: Path) -> None:
    _node_repo, _runtime_repo, _ledger_repo, ops = ops_setup
    _spec_id, _plan_id, task_id = _seed_task(ops)
    src = tmp_path / "shot.png"
    src.write_bytes(b"content")
    with pytest.raises(OperationError) as exc:
        ops.attach(task_id, src, replace="nope.png")
    assert exc.value.status_code == 404


def test_list_attachments_check_marks_fresh_stale_and_missing(
    ops_setup: tuple, tmp_path: Path
) -> None:
    _node_repo, _runtime_repo, _ledger_repo, ops = ops_setup
    _spec_id, _plan_id, task_id = _seed_task(ops)
    src = tmp_path / "checked.png"
    src.write_bytes(b"v1")
    entry = ops.attach(task_id, src)
    assert entry["source"]["state"] == "fresh"

    checked = ops.list_attachments(task_id, check=True)
    assert checked[0]["source"]["state"] == "fresh"
    assert checked[0]["source"]["checked_at"] is not None

    src.write_bytes(b"v2-changed")
    checked = ops.list_attachments(task_id, check=True)
    assert checked[0]["source"]["state"] == "stale"

    src.unlink()
    checked = ops.list_attachments(task_id, check=True)
    assert checked[0]["source"]["state"] == "missing"
