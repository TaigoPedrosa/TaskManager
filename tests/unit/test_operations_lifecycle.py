"""Operations on the lifecycle model: nodes start READY with their kind's flags, containers
roll up from their children, and every write passes the write rules before it commits."""

import subprocess
from pathlib import Path

import pytest

from taskmanager.core.enums import NodeKind, RenderView, VerificationType
from taskmanager.core.models import Lease, Node
from taskmanager.core.status import Action, ConditionStage, DecisionStatus, Merge, Outcome, Status
from taskmanager.db.connection import DatabaseManager
from taskmanager.db.job_repo import JobRepository
from taskmanager.db.ledger_repo import LedgerRepository
from taskmanager.db.node_repo import NodeRepository
from taskmanager.db.runtime_repo import RuntimeRepository
from taskmanager.engine.graph import GraphEngine
from taskmanager.engine.operations import OperationError, Operations
from taskmanager.engine.runtime import ExecutionCoordinator
from taskmanager.engine.verification import VerificationEngine
from taskmanager.renderers.markdown import MarkdownRenderer

Env = tuple[NodeRepository, RuntimeRepository, LedgerRepository, Operations]


def make_ops(root: Path) -> Env:
    db = DatabaseManager(root / ".taskmanager")
    db.init_all()
    node_repo = NodeRepository(db)
    runtime_repo = RuntimeRepository(db)
    ledger_repo = LedgerRepository(db)
    graph = GraphEngine(node_repo, runtime_repo)
    ops = Operations(
        node_repo,
        runtime_repo,
        graph,
        ExecutionCoordinator(node_repo, runtime_repo, graph),
        ledger_repo,
        VerificationEngine(root),
        actor="tester",
        job_repo=JobRepository(db),
    )
    return node_repo, runtime_repo, ledger_repo, ops


@pytest.fixture
def env(tmp_path: Path) -> Env:
    return make_ops(tmp_path)


def get(repo: NodeRepository, node_id: str) -> Node:
    node = repo.get_node(node_id)
    assert node is not None
    return node


def events(ledger: LedgerRepository) -> int:
    return len(ledger.list_events(limit=10_000))


def set_status(repo: NodeRepository, node_id: str, status: Status) -> None:
    node = get(repo, node_id)
    node.status = status
    repo.save_node(node)


def tree(ops: Operations, **plan_flags: object) -> tuple[str, str, str]:
    spec = ops.add_spec("S", slug="S1")
    plan = ops.add_plan("P", spec, slug="P1", **plan_flags)  # type: ignore[arg-type]
    task = ops.add_task("T", plan, slug="T1")
    return spec, plan, task


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def repo_with_origin(root: Path, name: str) -> Path:
    origin = root / f"{name}.git"
    git(root, "init", "--bare", "-b", "main", str(origin))
    work = root / name
    git(root, "clone", str(origin), str(work))
    git(work, "config", "user.email", "ci@example.com")
    git(work, "config", "user.name", "CI")
    git(work, "commit", "--allow-empty", "-m", "init")
    git(work, "push", "origin", "HEAD:main")
    git(work, "fetch", "origin")
    return work


def test_new_nodes_start_ready_with_the_flags_of_their_kind(env: Env) -> None:
    node_repo, _runtime, _ledger, ops = env
    spec, plan, task = tree(ops)
    s, p, t = get(node_repo, spec), get(node_repo, plan), get(node_repo, task)
    assert (s.status, s.review, s.fix, s.merge) == (Status.READY, False, False, Merge.MAIN)
    assert (p.status, p.review, p.fix, p.merge) == (Status.READY, False, False, Merge.MAIN)
    assert (t.status, t.review, t.fix, t.merge) == (Status.READY, True, True, Merge.MAIN)


def test_add_plan_with_review_stores_the_flags_and_creates_no_gate_node(env: Env) -> None:
    node_repo, _runtime, _ledger, ops = env
    spec = ops.add_spec("S", slug="S1")
    plan = ops.add_plan("P", spec, slug="P1", review=True, fix=True)
    stored = get(node_repo, plan)
    assert (stored.review, stored.fix) == (True, True)
    assert node_repo.get_children(plan) == []
    assert node_repo.get_node(f"{plan}-REV") is None


@pytest.mark.parametrize(
    ("target", "changes"),
    [
        ("task", {"review": False}),
        ("task", {"fix": False}),
        ("spec", {"merge": Merge.PARENT}),
    ],
    ids=["fix-without-review", "review-without-fix-landing-on-main", "spec-on-a-parent"],
)
def test_update_refuses_a_flag_combination_the_write_rules_forbid(
    env: Env, target: str, changes: dict[str, object]
) -> None:
    node_repo, _runtime, ledger, ops = env
    spec, _plan, task = tree(ops)
    node_id = {"spec": spec, "task": task}[target]
    before = get(node_repo, node_id)
    count = events(ledger)
    with pytest.raises(OperationError) as exc:
        ops.update_node(node_id, **changes)  # type: ignore[arg-type]
    assert exc.value.status_code == 400
    after = get(node_repo, node_id)
    assert (after.review, after.fix, after.merge) == (before.review, before.fix, before.merge)
    assert events(ledger) == count


def test_landing_on_a_parent_is_refused_for_a_node_with_no_parent(env: Env) -> None:
    node_repo, _runtime, _ledger, ops = env
    node_repo.save_node(Node(id="LONE", kind=NodeKind.TASK, title="alone", status=Status.READY))
    with pytest.raises(OperationError) as exc:
        ops.update_node("LONE", merge=Merge.PARENT)
    assert exc.value.status_code == 400
    assert get(node_repo, "LONE").merge == Merge.MAIN


def test_review_without_fix_is_allowed_landing_where_the_parent_reviews_and_fixes(
    env: Env,
) -> None:
    node_repo, _runtime, _ledger, ops = env
    _spec, _plan, task = tree(ops, review=True, fix=True)
    ops.update_node(task, merge=Merge.PARENT, fix=False)
    stored = get(node_repo, task)
    assert (stored.review, stored.fix, stored.merge) == (True, False, Merge.PARENT)


def test_a_parent_write_that_leaves_a_child_rejection_unfixed_is_refused(env: Env) -> None:
    node_repo, _runtime, _ledger, ops = env
    _spec, plan, task = tree(ops, review=True, fix=True)
    ops.update_node(task, merge=Merge.PARENT, fix=False)
    with pytest.raises(OperationError) as exc:
        ops.update_node(plan, fix=False)
    assert exc.value.status_code == 400
    assert get(node_repo, plan).fix is True


def test_landing_on_a_parent_is_refused_for_a_test_command_naming_origin_main(env: Env) -> None:
    node_repo, _runtime, _ledger, ops = env
    _spec, _plan, task = tree(ops)
    ops.add_verification(
        task, VerificationType.TEST_COMMAND, "reads main", "git show origin/main:README.md"
    )
    with pytest.raises(OperationError) as exc:
        ops.update_node(task, merge=Merge.PARENT)
    assert exc.value.status_code == 400
    assert get(node_repo, task).merge == Merge.MAIN


def test_a_new_child_under_a_completed_plan_is_refused(env: Env) -> None:
    node_repo, _runtime, ledger, ops = env
    _spec, plan, _task = tree(ops)
    set_status(node_repo, plan, Status.COMPLETED)
    count = events(ledger)
    with pytest.raises(OperationError) as exc:
        ops.add_task("Late", plan, slug="LATE")
    assert exc.value.status_code == 409
    assert node_repo.get_node(f"{plan}-LATE") is None
    assert events(ledger) == count


def test_a_new_child_under_a_plan_waiting_to_land_sends_the_plan_back_to_ready(
    env: Env,
) -> None:
    node_repo, _runtime, _ledger, ops = env
    _spec, plan, task = tree(ops)
    set_status(node_repo, task, Status.COMPLETED)
    set_status(node_repo, plan, Status.IMPLEMENTED)
    ops.add_task("Late", plan, slug="LATE")
    assert get(node_repo, plan).status == Status.READY


def test_supersede_is_refused_while_the_node_holds_a_live_lease(env: Env) -> None:
    node_repo, runtime_repo, ledger, ops = env
    _spec, plan, task = tree(ops)
    other = ops.add_task("Other", plan, slug="T2")
    claimed = get(node_repo, task)
    claimed.status = Status.IMPLEMENTING
    claimed.claimed_from = Status.READY
    lease = Lease(
        task_id=task,
        agent_id="a",
        session_id="s",
        branch_name=f"tm/{task}",
        action=Action.IMPLEMENT,
        ttl_seconds=3600,
    )
    assert runtime_repo.claim(lease, [], claimed)
    count = events(ledger)
    with pytest.raises(OperationError) as exc:
        ops.supersede(task, other)
    assert exc.value.status_code == 409
    assert get(node_repo, task).status == Status.IMPLEMENTING
    assert runtime_repo.get_lease(task) is not None
    assert events(ledger) == count


def test_a_dependency_that_closes_a_cycle_is_refused_with_the_cycle_path(env: Env) -> None:
    node_repo, _runtime, ledger, ops = env
    _spec, plan, first = tree(ops)
    second = ops.add_task("Second", plan, slug="T2", depends_on=[first])
    count = events(ledger)
    with pytest.raises(OperationError) as exc:
        ops.set_dependencies(first, [second], [])
    assert exc.value.status_code == 409
    message = str(exc.value)
    assert "←" in message and f"{first}.start" in message
    assert node_repo.get_dependencies(first) == []
    assert events(ledger) == count


def test_a_task_depending_on_its_own_plan_is_refused_as_a_cycle(env: Env) -> None:
    node_repo, _runtime, _ledger, ops = env
    _spec, plan, _task = tree(ops)
    with pytest.raises(OperationError) as exc:
        ops.add_task("Loop", plan, slug="LOOP", depends_on=[plan])
    assert exc.value.status_code == 409
    assert node_repo.get_node(f"{plan}-LOOP") is None


def test_set_dependencies_takes_bare_ids_and_returns_the_edges_left(env: Env) -> None:
    node_repo, _runtime, _ledger, ops = env
    _spec, plan, first = tree(ops)
    second = ops.add_task("Second", plan, slug="T2")
    assert ops.set_dependencies(second, [first], []) == [first]
    assert ops.set_dependencies(second, [], [first]) == []
    assert node_repo.get_dependencies(second) == []


def test_moving_a_branch_cut_from_its_parent_onto_main_is_refused(env: Env, tmp_path: Path) -> None:
    node_repo, _runtime, _ledger, ops = env
    work = repo_with_origin(tmp_path, "core")
    _spec, plan, task = tree(ops, review=True, fix=True)
    ops.update_node(task, repo="core", merge=Merge.PARENT)
    git(work, "branch", "--no-track", f"tm/{plan}", "origin/main")
    git(work, "checkout", f"tm/{plan}")
    git(work, "commit", "--allow-empty", "-m", "container work")
    git(work, "branch", "--no-track", f"tm/{task}", f"tm/{plan}")
    git(work, "checkout", "--detach")
    with pytest.raises(OperationError) as exc:
        ops.update_node(task, merge=Merge.MAIN)
    assert exc.value.status_code == 409
    assert get(node_repo, task).merge == Merge.PARENT


def test_moving_a_branch_onto_a_parent_forked_from_the_same_commit_is_allowed(
    env: Env, tmp_path: Path
) -> None:
    node_repo, _runtime, _ledger, ops = env
    work = repo_with_origin(tmp_path, "core")
    _spec, plan, task = tree(ops, review=True, fix=True)
    ops.update_node(task, repo="core")
    git(work, "branch", "--no-track", f"tm/{task}", "origin/main")
    git(work, "branch", "--no-track", f"tm/{plan}", "origin/main")
    ops.update_node(task, merge=Merge.PARENT)
    assert get(node_repo, task).merge == Merge.PARENT


def test_superseding_the_last_unfinished_child_rolls_the_plan_up_to_implemented(
    env: Env,
) -> None:
    node_repo, _runtime, _ledger, ops = env
    _spec, plan, done = tree(ops)
    extra = ops.add_task("Extra", plan, slug="T2")
    set_status(node_repo, done, Status.COMPLETED)
    ops.supersede(extra, done, "none")
    assert get(node_repo, extra).status == Status.SUPERSEDED
    assert get(node_repo, plan).status == Status.IMPLEMENTED


def test_moving_a_task_rolls_up_the_plan_it_leaves_and_reopens_the_one_it_joins(
    env: Env,
) -> None:
    node_repo, _runtime, _ledger, ops = env
    spec, plan, done = tree(ops)
    moving = ops.add_task("Moving", plan, slug="T2")
    other = ops.add_plan("Other", spec, slug="P2")
    finished = ops.add_task("Finished", other, slug="T3")
    set_status(node_repo, done, Status.COMPLETED)
    set_status(node_repo, finished, Status.COMPLETED)
    set_status(node_repo, other, Status.IMPLEMENTED)
    ops.move_task(moving, other)
    assert get(node_repo, plan).status == Status.IMPLEMENTED
    assert get(node_repo, other).status == Status.READY


def test_land_order_is_refused_on_a_task(env: Env) -> None:
    _node_repo, _runtime, _ledger, ops = env
    _spec, _plan, task = tree(ops)
    with pytest.raises(OperationError) as exc:
        ops.update_node(task, land_order=["api"])
    assert exc.value.status_code == 400


def test_add_condition_stores_an_executable_check_and_remove_drops_it(env: Env) -> None:
    node_repo, _runtime, ledger, ops = env
    _spec, _plan, task = tree(ops)
    added = ops.add_condition(
        task, "staging is up", "curl -fsS https://staging.example/health", ConditionStage.LANDING
    )
    assert [(c.needs, c.stage) for c in node_repo.get_conditions(task)] == [
        ("staging is up", ConditionStage.LANDING)
    ]
    count = events(ledger)
    ops.remove_condition(task, added.idx)
    assert node_repo.get_conditions(task) == []
    assert events(ledger) == count + 1


@pytest.mark.parametrize("command", ["", "the design is signed off"], ids=["empty", "prose"])
def test_a_condition_without_an_executable_command_is_refused(env: Env, command: str) -> None:
    node_repo, _runtime, _ledger, ops = env
    _spec, _plan, task = tree(ops)
    with pytest.raises(OperationError) as exc:
        ops.add_condition(task, "sign-off", command)
    assert exc.value.status_code == 400
    assert "decision" in str(exc.value)
    assert node_repo.get_conditions(task) == []


def test_removing_a_condition_that_is_not_there_is_refused(env: Env) -> None:
    _node_repo, _runtime, _ledger, ops = env
    _spec, _plan, task = tree(ops)
    with pytest.raises(OperationError) as exc:
        ops.remove_condition(task, 7)
    assert exc.value.status_code == 404


def test_decisions_are_stored_in_their_own_vocabulary(env: Env) -> None:
    node_repo, _runtime, _ledger, ops = env
    _spec, _plan, task = tree(ops)
    decision = ops.add_decision("Which way?", slug="way", options=["a|A"], blocks=[task])
    assert get(node_repo, decision).status == DecisionStatus.OPEN
    ops.answer_decision(decision, option="a")
    assert get(node_repo, decision).status == DecisionStatus.ANSWERED
    ops.reopen_decision(decision)
    assert get(node_repo, decision).status == DecisionStatus.OPEN
    ops.withdraw_decision(decision, "moot")
    assert get(node_repo, decision).status == DecisionStatus.WITHDRAWN


def test_a_plans_brief_lists_the_children_whose_review_rejected(env: Env) -> None:
    node_repo, _runtime, _ledger, ops = env
    _spec, plan, rejected = tree(ops, review=True, fix=True)
    approved = ops.add_task("Approved", plan, slug="T2")
    ops.update_node(rejected, merge=Merge.PARENT, fix=False)
    for node_id, outcome in ((rejected, Outcome.REJECT), (approved, Outcome.APPROVE)):
        node = get(node_repo, node_id)
        node.status, node.outcome = Status.COMPLETED, outcome
        node_repo.save_node(node)
    renderer = MarkdownRenderer(node_repo)

    brief = renderer.render(plan, RenderView.SUBAGENT)
    assert "### Children whose review rejected" in brief
    assert f"- `{rejected}` (T): `tm section get {rejected}:review`" in brief
    assert f"`{approved}`" not in brief
    assert "Children whose review rejected" not in renderer.render(rejected, RenderView.SUBAGENT)
    assert "Children whose review rejected" not in renderer.render(plan, RenderView.FULL)
