"""Operations on the lifecycle model: nodes start READY with their kind's flags, containers
roll up from their children, and every write passes the write rules before it commits."""

import sqlite3
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from taskmanager.cli.main import app as cli_app
from taskmanager.core.enums import NodeKind, RenderView, VerificationType
from taskmanager.core.lifecycle import next_action
from taskmanager.core.models import Lease, Node
from taskmanager.core.status import Action, ConditionStage, DecisionStatus, Merge, Outcome, Status
from taskmanager.db.connection import DatabaseManager
from taskmanager.db.job_repo import JobRepository
from taskmanager.db.ledger_repo import LedgerRepository
from taskmanager.db.node_repo import NodeRepository
from taskmanager.db.runtime_repo import RuntimeRepository
from taskmanager.engine.operations import OperationError, Operations
from taskmanager.engine.verification import VerificationEngine
from taskmanager.renderers.markdown import MarkdownRenderer

Env = tuple[NodeRepository, RuntimeRepository, LedgerRepository, Operations]


def make_ops(root: Path) -> Env:
    db = DatabaseManager(root / ".taskmanager")
    db.init_all()
    node_repo = NodeRepository(db)
    runtime_repo = RuntimeRepository(db)
    ledger_repo = LedgerRepository(db)
    ops = Operations(
        node_repo,
        runtime_repo,
        ledger_repo,
        VerificationEngine(root),
        JobRepository(db),
        actor="tester",
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
    assert (s.status, s.review, s.fix, s.merge) == (Status.READY, False, False, Merge.SPEC)
    assert (p.status, p.review, p.fix, p.merge) == (Status.READY, False, False, Merge.SPEC)
    assert (t.status, t.review, t.fix, t.merge) == (Status.READY, True, True, Merge.SPEC)


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
    assert get(node_repo, "LONE").merge == Merge.SPEC


def test_review_without_fix_is_allowed_landing_where_the_parent_reviews_and_fixes(
    env: Env,
) -> None:
    node_repo, _runtime, _ledger, ops = env
    _spec, _plan, task = tree(ops, review=True, fix=True)
    ops.update_node(task, review=True, merge=Merge.PARENT, fix=False)
    stored = get(node_repo, task)
    assert (stored.review, stored.fix, stored.merge) == (True, False, Merge.PARENT)


def test_a_parent_write_that_leaves_a_child_rejection_unfixed_is_refused(env: Env) -> None:
    node_repo, _runtime, _ledger, ops = env
    _spec, plan, task = tree(ops, review=True, fix=True)
    ops.update_node(task, review=True, merge=Merge.PARENT, fix=False)
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
    assert get(node_repo, task).merge == Merge.SPEC


@pytest.mark.parametrize("landed", [Status.COMPLETED, Status.LANDED])
def test_a_new_child_under_a_plan_whose_code_landed_is_refused(env: Env, landed: Status) -> None:
    node_repo, _runtime, ledger, ops = env
    _spec, plan, _task = tree(ops)
    set_status(node_repo, plan, landed)
    count = events(ledger)
    with pytest.raises(OperationError, match=f"{plan} is {landed}; file a new plan") as exc:
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
        ops.update_node(task, merge=Merge.SPEC)
    assert exc.value.status_code == 409
    assert get(node_repo, task).merge == Merge.PARENT


def test_moving_a_landed_branch_is_refused_as_landed_code(env: Env, tmp_path: Path) -> None:
    node_repo, _runtime, _ledger, ops = env
    work = repo_with_origin(tmp_path, "core")
    _spec, plan, task = tree(ops, review=True, fix=True)
    ops.update_node(task, repo="core", merge=Merge.PARENT)
    git(work, "branch", "--no-track", f"tm/{plan}", "origin/main")
    git(work, "checkout", f"tm/{plan}")
    git(work, "commit", "--allow-empty", "-m", "container work")
    git(work, "branch", "--no-track", f"tm/{task}", f"tm/{plan}")
    git(work, "checkout", "--detach")
    set_status(node_repo, task, Status.LANDED)
    with pytest.raises(OperationError, match="its code has landed; file a new task") as exc:
        ops.update_node(task, merge=Merge.SPEC)
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
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    node_repo, _runtime, _ledger, ops = env
    monkeypatch.setattr(ops, "nothing_to_land", lambda _container: False)
    _spec, plan, done = tree(ops)
    extra = ops.add_task("Extra", plan, slug="T2")
    set_status(node_repo, done, Status.COMPLETED)
    ops.supersede(extra, done, "none")
    assert get(node_repo, extra).status == Status.SUPERSEDED
    assert get(node_repo, plan).status == Status.IMPLEMENTED


def test_moving_a_task_rolls_up_the_plan_it_leaves_and_reopens_the_one_it_joins(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    node_repo, _runtime, _ledger, ops = env
    monkeypatch.setattr(ops, "nothing_to_land", lambda _container: False)
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
    ops.update_node(rejected, review=True, merge=Merge.PARENT, fix=False)
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


def test_a_move_that_leaves_a_plan_only_deferred_children_strands_its_dependents(
    env: Env,
) -> None:
    node_repo, _runtime, ledger, ops = env
    spec, plan, deferred = tree(ops)
    leaving = ops.add_task("Leaving", plan, slug="T2")
    other = ops.add_plan("Other", spec, slug="P2")
    waiting = ops.add_task("Waiting", other, slug="W", depends_on=[plan])
    set_status(node_repo, deferred, Status.DEFERRED)

    ops.move_task(leaving, other)

    assert get(node_repo, plan).status == Status.DEFERRED
    decisions = [
        d
        for d in node_repo.get_dependencies(waiting)
        if get(node_repo, d).kind == NodeKind.DECISION
    ]
    assert [get(node_repo, d).title for d in decisions] == [
        f"{plan} was DEFERRED: drop the edge, defer, or abandon the dependents?"
    ]
    rolled = [e for e in ledger.list_events(limit=10_000) if e.command == "rollup"]
    assert [(e.target_id, e.payload["to"]) for e in rolled] == [(plan, Status.DEFERRED)]


def test_a_move_completes_a_plan_left_with_nothing_to_land_and_keeps_one_with_code(
    env: Env, tmp_path: Path
) -> None:
    node_repo, _runtime, _ledger, ops = env
    work = repo_with_origin(tmp_path, "core")
    spec = ops.add_spec("S", slug="S1")
    empty, full = ops.add_plan("Empty", spec, slug="E"), ops.add_plan("Full", spec, slug="F")
    parked = ops.add_plan("Parked", spec, slug="Z")
    for plan in (empty, full):
        for slug in ("done", "moving"):
            ops.update_node(ops.add_task(slug, plan, slug=slug), repo="core")
        set_status(node_repo, f"{plan}-done", Status.COMPLETED)
    git(work, "checkout", "-q", "-b", f"tm/{full}", "origin/main")
    (work / "code.py").write_text("x = 1\n")
    git(work, "add", "code.py")
    git(work, "commit", "-q", "-m", "container work")
    git(work, "checkout", "-q", "--detach")

    ops.move_task(f"{empty}-moving", parked)
    ops.move_task(f"{full}-moving", parked)

    assert get(node_repo, empty).status == Status.COMPLETED
    assert get(node_repo, full).status == Status.IMPLEMENTED


def test_a_plan_whose_repository_is_not_cloned_is_not_read_as_having_nothing_to_land(
    env: Env,
) -> None:
    node_repo, _runtime, _ledger, ops = env
    _spec, plan, task = tree(ops)
    ops.update_node(task, repo="ghost")
    extra = ops.add_task("Extra", plan, slug="T2")
    set_status(node_repo, task, Status.COMPLETED)
    ops.supersede(extra, task, "none")
    assert get(node_repo, plan).status == Status.IMPLEMENTED


def test_a_plan_whose_tasks_name_no_repository_is_not_read_as_having_nothing_to_land(
    env: Env,
) -> None:
    node_repo, _runtime, _ledger, ops = env
    _spec, plan, task = tree(ops)
    extra = ops.add_task("Extra", plan, slug="T2")
    set_status(node_repo, task, Status.COMPLETED)
    ops.supersede(extra, task, "none")
    assert get(node_repo, plan).status == Status.IMPLEMENTED


def test_an_update_racing_a_claim_keeps_the_claim_s_status(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    node_repo, runtime_repo, _ledger, ops = env
    _spec, _plan, task = tree(ops)
    real = node_repo.transaction

    def claimed_first() -> object:
        monkeypatch.setattr(node_repo, "transaction", real)
        claimed = get(node_repo, task).model_copy(
            update={"status": Status.IMPLEMENTING, "claimed_from": Status.READY}
        )
        lease = Lease(
            task_id=task, agent_id="a", session_id="s", branch_name=f"tm/{task}", ttl_seconds=600
        )
        assert runtime_repo.claim(lease, [], claimed)
        return real()

    monkeypatch.setattr(node_repo, "transaction", claimed_first)
    ops.update_node(task, title="Renamed")

    node = get(node_repo, task)
    assert (node.title, node.status, node.claimed_from) == (
        "Renamed",
        Status.IMPLEMENTING,
        Status.READY,
    )


def test_an_open_write_transaction_holds_the_write_lock_before_its_first_write(
    tmp_path: Path,
) -> None:
    import sqlite3

    db = DatabaseManager(tmp_path / ".taskmanager")
    db.init_all()
    other = sqlite3.connect(str(db.state_db), timeout=0)
    with db.spec_transaction(), pytest.raises(sqlite3.OperationalError, match="locked"):
        other.execute("BEGIN IMMEDIATE")
    other.close()


def test_a_set_aside_child_s_repository_does_not_keep_its_plan_from_completing(
    env: Env, tmp_path: Path
) -> None:
    node_repo, _runtime, _ledger, ops = env
    repo_with_origin(tmp_path, "core")
    _spec, plan, task = tree(ops)
    ops.update_node(task, repo="core")
    ghost = ops.add_task("Ghost", plan, slug="T2")
    ops.update_node(ghost, repo="ghost")
    extra = ops.add_task("Extra", plan, slug="T3")
    set_status(node_repo, task, Status.COMPLETED)
    set_status(node_repo, ghost, Status.ABANDONED)
    ops.supersede(extra, task, "none")
    assert get(node_repo, plan).status == Status.COMPLETED


def test_a_ledger_write_failing_after_its_transaction_commits_neither_fails_it_nor_drops_the_next(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    node_repo, _runtime, ledger, ops = env
    _spec, _plan, task = tree(ops)
    count = events(ledger)
    real = ledger.db.get_ledger_connection
    calls: list[int] = []

    def full_disk() -> object:
        calls.append(1)
        if len(calls) == 1:
            raise sqlite3.OperationalError("database or disk is full")
        return real()

    monkeypatch.setattr(ledger.db, "get_ledger_connection", full_disk)
    with node_repo.transaction():
        ops.update_node(task, title="first")
        ops.update_node(task, title="second")

    assert get(node_repo, task).title == "second"
    assert len(calls) == 2
    assert events(ledger) == count + 1


def test_an_update_naming_a_sensitive_area_outside_the_four_is_refused_with_its_name(
    env: Env,
) -> None:
    node_repo, _runtime, ledger, ops = env
    _spec, _plan, task = tree(ops)
    count = events(ledger)
    with pytest.raises(OperationError, match=f"{task}: sensitive names 'pii'") as exc:
        ops.update_node(task, frontmatter_set={"sensitive": ["rls", "pii"]})
    assert exc.value.status_code == 400
    assert "sensitive" not in get(node_repo, task).frontmatter
    assert events(ledger) == count


@pytest.mark.parametrize(
    ("value", "stored"),
    [("migration", "migration"), ('["crypto","tenant"]', ["crypto", "tenant"])],
)
def test_task_update_set_takes_a_known_sensitive_area_and_refuses_another(
    tmp_path: Path, value: str, stored: object
) -> None:
    node_repo, _runtime, _ledger, ops = make_ops(tmp_path)
    _spec, _plan, task = tree(ops)

    def update(raw: str) -> tuple[int, str]:
        result = CliRunner().invoke(
            cli_app, ["task", "update", task, "--set", f"sensitive={raw}", "-C", str(tmp_path)]
        )
        return result.exit_code, result.output

    assert update(value)[0] == 0
    assert get(node_repo, task).frontmatter["sensitive"] == stored
    code, output = update("billing")
    assert code == 1
    assert "sensitive names 'billing'" in output
    assert get(node_repo, task).frontmatter["sensitive"] == stored


def test_an_update_declaring_a_migration_makes_the_node_s_fix_take_a_review(env: Env) -> None:
    node_repo, _runtime, _ledger, ops = env
    _spec, _plan, task = tree(ops)
    node = get(node_repo, task)
    node_repo.save_node(
        node.model_copy(
            update={"status": Status.FIXED, "outcome": Outcome.REJECT, "fix_for": Outcome.REJECT}
        )
    )
    assert next_action(ops.snapshots.cycle(get(node_repo, task))) == Action.MERGE

    ops.update_node(
        task, frontmatter_set={"declared_files": ["db/migrations/versions/0007_rls.py"]}
    )

    assert next_action(ops.snapshots.cycle(get(node_repo, task))) == Action.REVIEW


UNREVIEWED_ON_MAIN = (
    "lands where its spec lands with review off, so its code would land there unreviewed: "
    "S1-P1's review reads only what lands on its branch; set merge=parent, or turn review on"
)


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ({}, (False, False, Merge.PARENT)),
        ({"frontmatter": {"sensitive": ["crypto"]}}, (True, True, Merge.PARENT)),
        (
            {"frontmatter": {"declared_files": ["db/migrations/versions/0008_keys.py"]}},
            (True, True, Merge.PARENT),
        ),
        ({"review": True}, (True, False, Merge.PARENT)),
        ({"review": True, "fix": True, "merge": Merge.SPEC}, (True, True, Merge.SPEC)),
    ],
    ids=["plain", "sensitive-key", "writes-a-migration", "explicit-review", "explicit-main"],
)
def test_add_task_under_a_reviewed_plan_lands_on_its_branch_and_reviews_only_if_sensitive(
    env: Env, given: dict[str, object], expected: tuple[bool, bool, Merge]
) -> None:
    node_repo, _runtime, _ledger, ops = env
    spec = ops.add_spec("S", slug="S1")
    plan = ops.add_plan("P", spec, slug="P1", review=True, fix=True)
    task = ops.add_task("T", plan, slug="T1", **given)  # type: ignore[arg-type]
    stored = get(node_repo, task)
    assert (stored.review, stored.fix, stored.merge) == expected


def test_add_plan_under_a_reviewed_spec_lands_on_the_spec_s_branch(env: Env) -> None:
    node_repo, _runtime, _ledger, ops = env
    spec = ops.add_spec("S", slug="S1", review=True, fix=True)
    plan = get(node_repo, ops.add_plan("P", spec, slug="P1"))
    assert (plan.review, plan.fix, plan.merge) == (False, False, Merge.PARENT)


@pytest.mark.parametrize(
    "given",
    [{"merge": Merge.SPEC}, {"merge": Merge.SPEC, "review": False, "fix": False}],
    ids=["review-by-default", "review-stated-off"],
)
def test_add_task_landing_on_main_unreviewed_under_a_reviewed_plan_is_refused(
    env: Env, given: dict[str, object]
) -> None:
    node_repo, _runtime, ledger, ops = env
    spec = ops.add_spec("S", slug="S1")
    plan = ops.add_plan("P", spec, slug="P1", review=True, fix=True)
    count = events(ledger)
    with pytest.raises(OperationError, match=f"S1-P1-T1: {UNREVIEWED_ON_MAIN}") as exc:
        ops.add_task("T", plan, slug="T1", **given)  # type: ignore[arg-type]
    assert exc.value.status_code == 400
    assert node_repo.get_node("S1-P1-T1") is None
    assert events(ledger) == count


def test_an_update_sending_a_reviewed_plan_s_child_to_main_unreviewed_is_refused(
    env: Env,
) -> None:
    node_repo, _runtime, _ledger, ops = env
    _spec, _plan, task = tree(ops, review=True, fix=True)
    with pytest.raises(OperationError, match=f"{task}: {UNREVIEWED_ON_MAIN}"):
        ops.update_node(task, merge=Merge.SPEC)
    assert get(node_repo, task).merge == Merge.PARENT


def test_turning_review_on_over_a_child_landing_on_main_unreviewed_is_refused(env: Env) -> None:
    node_repo, _runtime, _ledger, ops = env
    _spec, plan, task = tree(ops)
    ops.update_node(task, review=False, fix=False)
    with pytest.raises(OperationError, match=f"{task}: {UNREVIEWED_ON_MAIN}"):
        ops.update_node(plan, review=True, fix=True)
    assert get(node_repo, plan).review is False


def test_a_child_already_landing_on_main_unreviewed_does_not_block_a_write_beside_it(
    env: Env,
) -> None:
    node_repo, _runtime, _ledger, ops = env
    _spec, plan, task = tree(ops, review=True, fix=True)
    node_repo.save_node(get(node_repo, task).model_copy(update={"merge": Merge.SPEC}))
    ops.update_node(plan, title="renamed")
    assert get(node_repo, plan).title == "renamed"
