import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from taskmanager.core.models import Lease
from taskmanager.core.status import Action, DecisionStatus, Status
from taskmanager.db.connection import DatabaseManager
from taskmanager.db.job_repo import JobRepository
from taskmanager.db.ledger_repo import LedgerRepository
from taskmanager.db.node_repo import NodeRepository
from taskmanager.db.runtime_repo import RuntimeRepository
from taskmanager.engine import operations as operations_module
from taskmanager.engine.decisions import DecisionOption, read_decision
from taskmanager.engine.operations import OperationError, Operations
from taskmanager.engine.verification import VerificationEngine


class Kit:
    def __init__(self, root: Path) -> None:
        self.root = root
        db = DatabaseManager(root / ".taskmanager")
        db.init_all()
        self.nodes = NodeRepository(db)
        self.runtime = RuntimeRepository(db)
        self.ops = Operations(
            self.nodes,
            self.runtime,
            LedgerRepository(db),
            VerificationEngine(root),
            JobRepository(db),
            actor="tester",
        )
        self.ops.add_spec("Spec", slug="S1")
        self.ops.add_plan("Plan", "S1", slug="P1")

    def title(self, node_id: str) -> str:
        node = self.nodes.get_node(node_id)
        assert node is not None
        return node.title

    def file(self, name: str, content: str) -> Path:
        path = self.root / name
        path.write_text(content)
        return path

    def interleave(self, monkeypatch: pytest.MonkeyPatch, rival: Callable[[], object]) -> None:
        """Runs `rival` once, at the first transaction or node save a write makes: after any
        read the write took before its transaction, before any read it takes inside one."""
        real_transaction, real_save = self.nodes.transaction, self.nodes.save_node
        fired: list[bool] = []

        def fire() -> None:
            if not fired:
                fired.append(True)
                rival()

        def transaction() -> Any:
            fire()
            return real_transaction()

        def save(*args: Any, **kwargs: Any) -> None:
            fire()
            real_save(*args, **kwargs)

        monkeypatch.setattr(self.nodes, "transaction", transaction)
        monkeypatch.setattr(self.nodes, "save_node", save)


@pytest.fixture
def kit(tmp_path: Path) -> Kit:
    return Kit(tmp_path)


Adder = Callable[[Operations, str, str | None], str]
ADDERS: dict[str, Adder] = {
    "spec": lambda ops, title, slug: ops.add_spec(title, slug=slug),
    "plan": lambda ops, title, slug: ops.add_plan(title, "S1", slug=slug),
    "task": lambda ops, title, slug: ops.add_task(title, "S1-P1", slug=slug),
    "decision": lambda ops, title, slug: ops.add_decision(title, slug=slug),
}


@pytest.mark.parametrize("kind", ADDERS)
def test_concurrent_adds_without_a_slug_take_distinct_ids(
    kit: Kit, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    add = ADDERS[kind]
    rival: list[str] = []
    kit.interleave(monkeypatch, lambda: rival.append(add(kit.ops, "rival", None)))
    mine = add(kit.ops, "mine", None)
    assert mine != rival[0]
    assert (kit.title(mine), kit.title(rival[0])) == ("mine", "rival")


@pytest.mark.parametrize("kind", ADDERS)
def test_concurrent_adds_of_one_slug_refuse_the_second(
    kit: Kit, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    add = ADDERS[kind]
    rival: list[str] = []
    kit.interleave(monkeypatch, lambda: rival.append(add(kit.ops, "rival", "same")))
    with pytest.raises(OperationError) as refused:
        add(kit.ops, "mine", "same")
    assert refused.value.status_code == 409
    assert kit.title(rival[0]) == "rival"


def test_add_task_without_a_slug_skips_the_id_of_a_task_moved_out(kit: Kit) -> None:
    kit.ops.add_plan("Other", "S1", slug="P2")
    first = kit.ops.add_task("first", "S1-P1")
    kit.ops.add_task("second", "S1-P1")
    kit.ops.move_task(first, "S1-P2")
    fresh = kit.ops.add_task("fresh", "S1-P1")
    assert fresh == "S1-P1-T3"
    assert kit.title(first) == "first"
    assert kit.nodes.get_parent_ids(first) == ["S1-P2"]


def test_add_task_refuses_a_parent_that_is_not_a_plan(kit: Kit) -> None:
    task = kit.ops.add_task("t", "S1-P1")
    for parent in ("S1", task, "nope"):
        with pytest.raises(OperationError) as refused:
            kit.ops.add_task("x", parent)
        assert refused.value.status_code == (404 if parent == "nope" else 400)
    assert kit.nodes.get_children("S1") == ["S1-P1"]


def test_add_plan_refuses_a_parent_that_is_not_a_spec(kit: Kit) -> None:
    task = kit.ops.add_task("t", "S1-P1")
    for parent in ("S1-P1", task):
        with pytest.raises(OperationError) as refused:
            kit.ops.add_plan("x", parent)
        assert refused.value.status_code == 400
    assert kit.nodes.get_children("S1-P1") == [task]


BAD_SLUGS = ["../../esc", "a/b", "/abs", "-x", ".x", "a..b", "..", "a b", "x\n"]


@pytest.mark.parametrize("slug", BAD_SLUGS)
@pytest.mark.parametrize("kind", ADDERS)
def test_a_slug_that_is_not_one_plain_path_component_is_refused(
    kit: Kit, kind: str, slug: str
) -> None:
    before = {n.id for n in kit.nodes.list_nodes()}
    with pytest.raises(OperationError) as refused:
        ADDERS[kind](kit.ops, "t", slug)
    assert refused.value.status_code == 400
    assert {n.id for n in kit.nodes.list_nodes()} == before


@pytest.mark.parametrize("kind", ADDERS)
def test_a_slug_of_letters_digits_dots_underscores_and_dashes_is_taken(kit: Kit, kind: str) -> None:
    node_id = ADDERS[kind](kit.ops, "t", "v1.2_a-b")
    assert node_id.endswith("v1.2_a-b")


def _decision_with_two_options(kit: Kit) -> str:
    return kit.ops.add_decision(
        "Q?",
        slug="q",
        options=[DecisionOption(key="a", label="A"), DecisionOption(key="b", label="B")],
    )


def test_a_second_answer_to_one_decision_is_refused(
    kit: Kit, monkeypatch: pytest.MonkeyPatch
) -> None:
    decision = _decision_with_two_options(kit)
    kit.interleave(monkeypatch, lambda: kit.ops.answer_decision(decision, option="a"))
    with pytest.raises(OperationError) as refused:
        kit.ops.answer_decision(decision, option="b")
    assert refused.value.status_code == 409
    node = kit.nodes.get_node(decision)
    assert node is not None
    answer = read_decision(node).answer
    assert answer is not None
    assert answer.option == "a"


def test_withdrawing_a_decision_answered_meanwhile_is_refused(
    kit: Kit, monkeypatch: pytest.MonkeyPatch
) -> None:
    decision = _decision_with_two_options(kit)
    kit.interleave(monkeypatch, lambda: kit.ops.answer_decision(decision, option="a"))
    with pytest.raises(OperationError) as refused:
        kit.ops.withdraw_decision(decision, reason="stale")
    assert refused.value.status_code == 409
    assert kit.nodes.get_node(decision).status == DecisionStatus.ANSWERED


def test_withdrawing_a_withdrawn_decision_is_refused(kit: Kit) -> None:
    decision = _decision_with_two_options(kit)
    kit.ops.withdraw_decision(decision, reason="first")
    with pytest.raises(OperationError) as refused:
        kit.ops.withdraw_decision(decision, reason="second")
    assert refused.value.status_code == 409
    node = kit.nodes.get_node(decision)
    assert read_decision(node).withdrawn_reason == "first"


def test_reopening_a_decision_reopened_meanwhile_is_refused(
    kit: Kit, monkeypatch: pytest.MonkeyPatch
) -> None:
    decision = _decision_with_two_options(kit)
    kit.ops.answer_decision(decision, option="a")
    kit.interleave(monkeypatch, lambda: kit.ops.reopen_decision(decision))
    with pytest.raises(OperationError) as refused:
        kit.ops.reopen_decision(decision)
    assert refused.value.status_code == 409


def _attachments(kit: Kit, node_id: str) -> list[str]:
    return [a["name"] for a in kit.ops.list_attachments(node_id)]


def test_concurrent_attaches_keep_both_entries(kit: Kit, monkeypatch: pytest.MonkeyPatch) -> None:
    task = kit.ops.add_task("t", "S1-P1")
    first, second = kit.file("one.txt", "1"), kit.file("two.txt", "2")
    kit.interleave(monkeypatch, lambda: kit.ops.attach(task, second))
    kit.ops.attach(task, first)
    assert sorted(_attachments(kit, task)) == ["one.txt", "two.txt"]


def test_an_update_does_not_write_back_an_attachment_made_meanwhile(
    kit: Kit, monkeypatch: pytest.MonkeyPatch
) -> None:
    task = kit.ops.add_task("t", "S1-P1")
    kit.interleave(monkeypatch, lambda: kit.ops.attach(task, kit.file("a.txt", "a")))
    kit.ops.update_node(task, frontmatter_set={"note": "x"})
    node = kit.nodes.get_node(task)
    assert node.frontmatter["note"] == "x"
    assert _attachments(kit, task) == ["a.txt"]


def test_an_attachment_check_does_not_write_back_an_attachment_made_meanwhile(
    kit: Kit, monkeypatch: pytest.MonkeyPatch
) -> None:
    task = kit.ops.add_task("t", "S1-P1")
    kit.ops.attach(task, kit.file("a.txt", "a"))
    kit.interleave(monkeypatch, lambda: kit.ops.attach(task, kit.file("b.txt", "b")))
    kit.ops.list_attachments(task, check=True)
    assert sorted(_attachments(kit, task)) == ["a.txt", "b.txt"]


def test_a_detach_cannot_delete_a_file_an_attach_stored_but_not_yet_entered(
    kit: Kit, monkeypatch: pytest.MonkeyPatch
) -> None:
    holder = kit.ops.add_task("holder", "S1-P1")
    newcomer = kit.ops.add_task("newcomer", "S1-P1")
    path = kit.file("same.txt", "same bytes")
    asset = kit.ops.attach(holder, path)["asset"]
    real_store = operations_module.store_asset
    detached: list[threading.Thread] = []

    def store_then_detach(assets_dir: Path, source: Path) -> tuple[str, str]:
        stored = real_store(assets_dir, source)
        thread = threading.Thread(target=lambda: kit.ops.detach(holder, asset))
        thread.start()
        # A detach that is not held out by the attach's transaction finishes here; one that is
        # held out waits for the attach to commit, and the join only gives up.
        thread.join(timeout=1)
        detached.append(thread)
        return stored

    monkeypatch.setattr(operations_module, "store_asset", store_then_detach)
    kit.ops.attach(newcomer, path)
    detached[0].join(timeout=10)
    assert (kit.ops._assets_dir() / asset).is_file()
    assert _attachments(kit, newcomer) == ["same.txt"]


def test_detach_never_unlinks_a_name_that_is_not_a_stored_asset(kit: Kit) -> None:
    task = kit.ops.add_task("t", "S1-P1")
    outside = kit.root / ".taskmanager" / "outside.txt"
    outside.write_text("keep")
    kit.ops._assets_dir().mkdir(parents=True)
    planted = "../outside.txt"
    kit.ops.update_node(task, frontmatter_set={"attachments": [{"asset": planted}]})
    kit.ops.detach(task, planted)
    assert outside.read_text() == "keep"
    assert kit.ops.list_attachments(task) == []


def test_supersede_does_not_write_back_a_stale_copy_of_the_old_node(
    kit: Kit, monkeypatch: pytest.MonkeyPatch
) -> None:
    old = kit.ops.add_task("old", "S1-P1")
    new = kit.ops.add_task("new", "S1-P1")
    kit.interleave(monkeypatch, lambda: kit.ops.update_node(old, title="renamed"))
    kit.ops.supersede(old, new)
    assert kit.title(old) == "renamed"
    assert kit.nodes.get_node(old).status == Status.SUPERSEDED


def test_a_move_reads_the_old_parent_under_the_lock(
    kit: Kit, monkeypatch: pytest.MonkeyPatch
) -> None:
    kit.ops.add_plan("Second", "S1", slug="P2")
    kit.ops.add_plan("Third", "S1", slug="P3")
    task = kit.ops.add_task("t", "S1-P1")
    kit.interleave(monkeypatch, lambda: kit.ops.move_task(task, "S1-P3"))
    kit.ops.move_task(task, "S1-P2")
    assert kit.nodes.get_parent_ids(task) == ["S1-P2"]


@pytest.mark.parametrize("mode", ["nnoe", "al", "custom", ",", "nobody-waits", ""])
def test_supersede_refuses_a_transfer_that_names_no_waiting_node(kit: Kit, mode: str) -> None:
    old = kit.ops.add_task("old", "S1-P1")
    new = kit.ops.add_task("new", "S1-P1")
    waiter = kit.ops.add_task("waiter", "S1-P1", depends_on=[old])
    with pytest.raises(OperationError) as refused:
        kit.ops.supersede(old, new, transfer_blocks=mode)
    assert refused.value.status_code == 400
    assert kit.nodes.get_node(old).status != Status.SUPERSEDED
    assert kit.nodes.get_dependencies(waiter) == [old]


def test_supersede_moves_only_the_listed_waiting_nodes(kit: Kit) -> None:
    old = kit.ops.add_task("old", "S1-P1")
    new = kit.ops.add_task("new", "S1-P1")
    moved = kit.ops.add_task("moved", "S1-P1", depends_on=[old])
    kept = kit.ops.add_task("kept", "S1-P1", depends_on=[old])
    kit.ops.supersede(old, new, transfer_blocks=moved)
    assert kit.nodes.get_dependencies(moved) == [new]
    assert kit.nodes.get_dependencies(kept) == [old]


def _lease(task_id: str, action: Action) -> Lease:
    return Lease(
        task_id=task_id, agent_id="a", session_id="s", branch_name=f"tm/{task_id}", action=action
    )


def _declare(kit: Kit, task_id: str, *files: str) -> None:
    kit.ops.update_node(task_id, frontmatter_set={"declared_files": list(files)})


def _held(kit: Kit) -> dict[str, str]:
    return {lock.file_path: lock.task_id for lock in kit.runtime.list_locks()}


@pytest.mark.parametrize("action", [Action.IMPLEMENT, Action.FIX])
def test_a_file_declared_on_a_claimed_node_is_locked(kit: Kit, action: Action) -> None:
    task = kit.ops.add_task("t", "S1-P1")
    kit.runtime.acquire_lease(_lease(task, action), [])
    _declare(kit, task, "src/x.py")
    assert _held(kit) == {":src/x.py": task}


def test_a_file_held_by_another_live_node_is_refused_and_not_declared(kit: Kit) -> None:
    holder = kit.ops.add_task("holder", "S1-P1")
    task = kit.ops.add_task("t", "S1-P1")
    kit.runtime.acquire_lease(_lease(holder, Action.IMPLEMENT), [])
    _declare(kit, holder, "src/x.py")
    kit.runtime.acquire_lease(_lease(task, Action.IMPLEMENT), [])
    with pytest.raises(OperationError) as refused:
        _declare(kit, task, "src/y.py", "src/x.py")
    assert refused.value.status_code == 409
    assert "src/x.py" in str(refused.value)
    assert _held(kit) == {":src/x.py": holder}
    assert "declared_files" not in kit.nodes.get_node(task).frontmatter


def test_declaring_more_files_keeps_the_ones_the_node_already_holds(kit: Kit) -> None:
    task = kit.ops.add_task("t", "S1-P1")
    kit.runtime.acquire_lease(_lease(task, Action.IMPLEMENT), [])
    _declare(kit, task, "src/x.py")
    _declare(kit, task, "src/x.py", "src/y.py")
    assert _held(kit) == {":src/x.py": task, ":src/y.py": task}


@pytest.mark.parametrize("action", [None, Action.REVIEW])
def test_a_file_declared_without_an_implementing_lease_locks_nothing(
    kit: Kit, action: Action | None
) -> None:
    task = kit.ops.add_task("t", "S1-P1")
    if action is not None:
        kit.runtime.acquire_lease(_lease(task, action), [])
    _declare(kit, task, "src/x.py")
    assert _held(kit) == {}
