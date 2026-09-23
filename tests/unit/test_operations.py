from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from taskmanager.core.enums import NodeStatus, VerificationType
from taskmanager.core.models import Lease
from taskmanager.db.connection import DatabaseManager
from taskmanager.db.ledger_repo import LedgerRepository
from taskmanager.db.node_repo import NodeRepository
from taskmanager.db.runtime_repo import RuntimeRepository
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
