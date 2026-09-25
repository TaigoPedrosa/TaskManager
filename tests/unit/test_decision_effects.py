from collections.abc import Callable
from pathlib import Path

import pytest

from taskmanager.core.enums import NodeKind, RelationType
from taskmanager.core.models import Job, Lease, Node, NodeRelation
from taskmanager.core.status import DecisionEffect, DecisionStatus, JobKind, Outcome, Status
from taskmanager.db.connection import DatabaseManager
from taskmanager.db.job_repo import JobRepository
from taskmanager.db.ledger_repo import LedgerRepository
from taskmanager.db.node_repo import NodeRepository
from taskmanager.db.runtime_repo import RuntimeRepository
from taskmanager.engine.decisions import (
    DecisionOption,
    open_failed_decision,
    open_stranded_decision,
    read_decision,
    stranded_dependents,
)
from taskmanager.engine.operations import OperationError, Operations
from taskmanager.engine.verification import VerificationEngine


class Kit:
    def __init__(self, root: Path) -> None:
        db = DatabaseManager(root / ".taskmanager")
        db.init_all()
        self.nodes = NodeRepository(db)
        self.runtime = RuntimeRepository(db)
        self.jobs = JobRepository(db)
        self.ledger = LedgerRepository(db)
        self.ops = Operations(
            self.nodes,
            self.runtime,
            self.ledger,
            VerificationEngine(root),
            self.jobs,
            actor="tester",
        )

    def add(
        self,
        node_id: str,
        kind: NodeKind = NodeKind.TASK,
        parent: str | None = None,
        **fields: object,
    ) -> None:
        self.nodes.save_node(
            Node.model_validate({"id": node_id, "kind": kind, "title": node_id, **fields})
        )
        if parent is not None:
            self.nodes.add_relation(
                NodeRelation(
                    source_id=parent, target_id=node_id, relation_type=RelationType.CONTAINS
                )
            )

    def depend(self, dependent: str, dependency: str) -> None:
        self.nodes.add_relation(
            NodeRelation(
                source_id=dependent, target_id=dependency, relation_type=RelationType.DEPENDS_ON
            )
        )

    def node(self, node_id: str) -> Node:
        node = self.nodes.get_node(node_id)
        assert node is not None
        return node

    def section(self, node_id: str, key: str) -> str:
        section = self.nodes.get_section(node_id, key)
        return section.content if section else ""


@pytest.fixture
def kit(tmp_path: Path) -> Kit:
    return Kit(tmp_path)


@pytest.mark.parametrize("kind", [NodeKind.SPEC, NodeKind.PLAN, NodeKind.TASK])
def test_a_decision_blocks_any_node_that_is_not_a_decision(kit: Kit, kind: NodeKind) -> None:
    kit.add("N", kind, status=Status.READY)
    decision_id = kit.ops.add_decision("Which way?", slug="d1", blocks=["N"])
    assert kit.nodes.get_dependencies("N") == [decision_id]
    other = kit.ops.add_decision("Later?", slug="d2")
    kit.ops.link_decision(other, add=["N"])
    assert kit.nodes.get_dependencies("N") == [decision_id, other]


def test_a_decision_cannot_block_or_be_linked_to_another_decision(kit: Kit) -> None:
    first = kit.ops.add_decision("First?", slug="d1")
    before = len(kit.ledger.list_events(limit=1000))
    with pytest.raises(OperationError) as refused:
        kit.ops.add_decision("Second?", slug="d2", blocks=[first])
    assert refused.value.status_code == 400
    assert kit.nodes.get_node("decision-d2") is None
    second = kit.ops.add_decision("Second?", slug="d3")
    with pytest.raises(OperationError) as refused:
        kit.ops.link_decision(second, add=[first])
    assert refused.value.status_code == 400
    assert len(kit.ledger.list_events(limit=1000)) == before + 1


def _live_lease(kit: Kit) -> None:
    kit.runtime.acquire_lease(
        Lease(task_id="T", agent_id="a", session_id="s", branch_name="tm/T"), []
    )


def _running_job(kit: Kit) -> None:
    kit.jobs.create(Job(kind=JobKind.LAND, node_id="T", repo="core", target="main"))


@pytest.mark.parametrize("arrange", [_live_lease, _running_job], ids=["lease", "job"])
def test_a_decision_is_not_linked_to_a_node_mid_step(
    kit: Kit, arrange: Callable[[Kit], None]
) -> None:
    kit.add("T", status=Status.MERGING, claimed_from=Status.REVIEWED)
    arrange(kit)
    with pytest.raises(OperationError) as refused:
        kit.ops.add_decision("Q?", slug="d1", blocks=["T"])
    assert refused.value.status_code == 409
    decision_id = kit.ops.add_decision("Q?", slug="d2")
    with pytest.raises(OperationError) as refused:
        kit.ops.link_decision(decision_id, add=["T"])
    assert refused.value.status_code == 409
    assert kit.nodes.get_dependencies("T") == []


@pytest.mark.parametrize(
    ("raw", "effect"),
    [
        ("a|Abandon it", DecisionEffect.NONE),
        ("a|Abandon it|why", DecisionEffect.NONE),
        ("a|Abandon it|why|abandon", DecisionEffect.ABANDON),
        ("r|Reopen|why|reopen", DecisionEffect.REOPEN),
        ("x|Drop|why|drop_edge", DecisionEffect.DROP_EDGE),
    ],
)
def test_an_option_names_its_effect_in_a_fourth_field(
    kit: Kit, raw: str, effect: DecisionEffect
) -> None:
    decision_id = kit.ops.add_decision("Q?", slug="d1", options=[raw])
    assert read_decision(kit.node(decision_id)).options[0].effect == effect


def test_an_unknown_effect_is_refused(kit: Kit) -> None:
    with pytest.raises(OperationError, match="'explode' is not an effect") as refused:
        kit.ops.add_decision("Q?", slug="d1", options=["a|A|why|explode"])
    assert refused.value.status_code == 400
    assert kit.nodes.get_node("decision-d1") is None


@pytest.mark.parametrize(
    ("effect", "status", "section"),
    [
        ("abandon", Status.ABANDONED, "abandonment"),
        ("defer", Status.DEFERRED, "deferral"),
    ],
)
def test_answering_applies_the_options_effect_to_every_blocked_node(
    kit: Kit, effect: str, status: Status, section: str
) -> None:
    kit.add("P", NodeKind.PLAN, status=Status.READY)
    kit.add("T1", parent="P", status=Status.READY)
    kit.add("T2", parent="P", status=Status.REVIEWED, outcome=Outcome.REJECT)
    kit.add("T3", parent="P", status=Status.READY)
    decision_id = kit.ops.add_decision(
        "Drop these?", slug="d1", options=[f"go|Go|why|{effect}", "keep|Keep"], blocks=["T1", "T2"]
    )
    kit.ops.answer_decision(decision_id, option="go", text="scope cut", by="owner")
    assert (kit.node("T1").status, kit.node("T2").status, kit.node("T3").status) == (
        status,
        status,
        Status.READY,
    )
    assert kit.section("T1", section) == f"{decision_id} answered Go: scope cut"
    event = kit.ledger.list_events(target_id=decision_id, limit=1)[0]
    assert event.payload == {"option": "go", "effect": effect, "affected": ["T1", "T2"]}


def test_an_option_with_no_effect_changes_nothing(kit: Kit) -> None:
    kit.add("T", status=Status.READY)
    decision_id = kit.ops.add_decision("Q?", slug="d1", options=["k|Keep"], blocks=["T"])
    kit.ops.answer_decision(decision_id, option="k")
    assert kit.node("T").status == Status.READY


def test_reopen_returns_a_failed_task_to_ready_with_a_clean_cycle(kit: Kit) -> None:
    kit.add(
        "T",
        status=Status.FAILED,
        outcome=Outcome.REJECT,
        fix_for=Outcome.REJECT,
        verdict="still leaks the session",
        review_cycles=3,
        merge_attempts=1,
        step_failures=2,
        branch="tm/T",
    )
    decision_id = open_failed_decision(kit.ops, "T", "review rejected 3 times", "leaks")
    kit.ops.answer_decision(decision_id, option="investigate", text="split the session fix out")
    node = kit.node("T")
    assert node.status == Status.READY
    assert (node.outcome, node.fix_for, node.verdict) == (None, None, None)
    assert (node.review_cycles, node.merge_attempts, node.step_failures) == (0, 0, 0)
    assert node.branch == "tm/T"
    assert kit.section("T", "reopen") == (
        f"{decision_id} answered Investigate and reopen it: split the session fix out"
    )


def test_a_custom_answer_to_a_failed_decision_reopens_with_the_answer_as_its_note(
    kit: Kit,
) -> None:
    kit.add("T", status=Status.FAILED)
    decision_id = open_failed_decision(kit.ops, "T", "3 step failures", "")
    kit.ops.answer_decision(decision_id, text="retry once the runner is back")
    assert kit.node("T").status == Status.READY
    assert "retry once the runner is back" in kit.section("T", "reopen")


def test_reopening_a_container_whose_children_all_landed_returns_it_to_implemented(
    kit: Kit,
) -> None:
    kit.add("P", NodeKind.PLAN, status=Status.FAILED)
    kit.add("T1", parent="P", status=Status.COMPLETED)
    kit.add("T2", parent="P", status=Status.ABANDONED)
    decision_id = open_failed_decision(kit.ops, "P", "review rejected 4 times", "")
    kit.ops.answer_decision(decision_id, option="investigate")
    assert kit.node("P").status == Status.IMPLEMENTED


def test_a_failed_decision_blocks_its_node_and_says_why(kit: Kit) -> None:
    kit.add("T", status=Status.FAILED)
    decision_id = open_failed_decision(kit.ops, "T", "merge failed 3 times", "gate: 2 failed")
    decision = kit.node(decision_id)
    data = read_decision(decision)
    assert decision.title == "T failed: abandon, or investigate?"
    assert [(o.key, o.effect) for o in data.options] == [
        ("abandon", DecisionEffect.ABANDON),
        ("investigate", DecisionEffect.REOPEN),
    ]
    assert (data.subject, data.raised_by, data.custom_effect) == ("T", "T", DecisionEffect.REOPEN)
    assert kit.nodes.get_dependencies("T") == [decision_id]
    assert kit.section(decision_id, "context") == "merge failed 3 times\n\ngate: 2 failed"


def test_a_failed_node_opens_no_stranded_decision_until_it_is_abandoned(kit: Kit) -> None:
    kit.add("Y", status=Status.FAILED)
    kit.add("X1", status=Status.READY)
    kit.add("X2", status=Status.READY)
    kit.depend("X1", "Y")
    kit.depend("X2", "Y")
    failed = open_failed_decision(kit.ops, "Y", "review rejected", "")
    assert kit.nodes.get_blocked_by(failed) == ["Y"]
    kit.ops.answer_decision(failed, option="abandon")
    stranded = [d for d in kit.nodes.get_dependencies("X1") if d != "Y"]
    assert len(stranded) == 1
    decision = kit.node(stranded[0])
    assert decision.title == "Y was ABANDONED: drop the edge, defer, or abandon the dependents?"
    assert kit.nodes.get_blocked_by(stranded[0]) == ["X1", "X2"]


def test_dropping_the_edge_frees_every_stranded_dependent(kit: Kit) -> None:
    kit.add("Y", status=Status.DEFERRED)
    kit.add("X1", status=Status.READY)
    kit.add("X2", status=Status.READY)
    kit.depend("X1", "Y")
    kit.depend("X2", "Y")
    decision_id = open_stranded_decision(kit.ops, "Y", Status.DEFERRED, ["X1", "X2"])
    kit.ops.answer_decision(decision_id, option="drop_edge")
    assert kit.nodes.get_dependencies("X1") == [decision_id]
    assert kit.nodes.get_dependencies("X2") == [decision_id]
    assert (kit.node("X1").status, kit.node("X2").status) == (Status.READY, Status.READY)


def test_only_dependents_with_work_ahead_are_stranded(kit: Kit) -> None:
    kit.add("Y", status=Status.DEFERRED)
    for node_id, status in [
        ("READY", Status.READY),
        ("REVIEWED", Status.REVIEWED),
        ("FAILED", Status.FAILED),
        ("DONE", Status.COMPLETED),
        ("GONE", Status.ABANDONED),
        ("LATER", Status.DEFERRED),
    ]:
        outcome = Outcome.REJECT if status == Status.REVIEWED else None
        kit.add(node_id, status=status, outcome=outcome)
        kit.depend(node_id, "Y")
    assert stranded_dependents(kit.ops, "Y") == ["READY", "REVIEWED", "FAILED"]


def test_abandoning_a_plans_last_task_abandons_the_plan_and_strands_its_dependents(
    kit: Kit,
) -> None:
    kit.add("P", NodeKind.PLAN, status=Status.READY)
    kit.add("T", parent="P", status=Status.READY)
    kit.add("X", status=Status.READY)
    kit.depend("X", "P")
    decision_id = kit.ops.add_decision(
        "Drop T?", slug="d1", options=["y|Yes|why|abandon"], blocks=["T"]
    )
    kit.ops.answer_decision(decision_id, option="y")
    assert kit.node("P").status == Status.ABANDONED
    stranded = [d for d in kit.nodes.get_dependencies("X") if d != "P"]
    assert len(stranded) == 1
    assert kit.node(stranded[0]).title.startswith("P was ABANDONED")


@pytest.mark.parametrize(
    ("effect", "status"),
    [
        ("abandon", Status.COMPLETED),
        ("defer", Status.COMPLETED),
        ("reopen", Status.READY),
        ("abandon", Status.SUPERSEDED),
    ],
)
def test_an_effect_the_node_cannot_take_refuses_the_whole_answer(
    kit: Kit, effect: str, status: Status
) -> None:
    kit.add("T", status=status)
    kit.add("U", status=Status.FAILED)
    decision_id = kit.ops.add_decision(
        "Q?", slug="d1", options=[f"go|Go|why|{effect}"], blocks=["U", "T"]
    )
    before = len(kit.ledger.list_events(limit=1000))
    with pytest.raises(OperationError) as refused:
        kit.ops.answer_decision(decision_id, option="go")
    assert refused.value.status_code == 409
    assert kit.node(decision_id).status == DecisionStatus.OPEN
    assert (kit.node("T").status, kit.node("U").status) == (status, Status.FAILED)
    assert kit.nodes.get_all_sections("U") == []
    assert len(kit.ledger.list_events(limit=1000)) == before


def test_an_effect_waits_for_a_step_that_started_after_the_link(kit: Kit) -> None:
    kit.add("T", status=Status.READY)
    decision_id = kit.ops.add_decision("Q?", slug="d1", options=["go|Go|why|defer"], blocks=["T"])
    _live_lease(kit)
    with pytest.raises(OperationError, match="while a step runs") as refused:
        kit.ops.answer_decision(decision_id, option="go")
    assert refused.value.status_code == 409
    assert kit.node("T").status == Status.READY
    assert kit.node(decision_id).status == DecisionStatus.OPEN


def test_a_reopen_waits_while_another_decision_is_open_on_the_node(kit: Kit) -> None:
    kit.add("T", status=Status.FAILED)
    failed = open_failed_decision(kit.ops, "T", "3 step failures", "")
    kit.ops.add_decision("Budget?", slug="budget", blocks=["T"])
    with pytest.raises(OperationError, match="decision-budget is open"):
        kit.ops.answer_decision(failed, option="investigate")
    assert kit.node("T").status == Status.FAILED


def test_an_option_object_keeps_its_effect(kit: Kit) -> None:
    decision_id = kit.ops.add_decision(
        "Q?",
        slug="d1",
        options=[DecisionOption(key="a", label="A", effect=DecisionEffect.DEFER)],
    )
    assert read_decision(kit.node(decision_id)).options[0].effect == DecisionEffect.DEFER


def test_a_defer_answer_leaves_a_dependent_already_deferred_and_defers_the_rest(kit: Kit) -> None:
    kit.add("Y", status=Status.ABANDONED)
    kit.add("X1", status=Status.READY)
    kit.add("X2", status=Status.READY)
    decision_id = open_stranded_decision(kit.ops, "Y", Status.ABANDONED, ["X1", "X2"])
    kit.nodes.save_node(kit.node("X1").model_copy(update={"status": Status.DEFERRED}))
    kit.ops.answer_decision(decision_id, option="defer")
    assert (kit.node("X1").status, kit.node("X2").status) == (Status.DEFERRED, Status.DEFERRED)
    assert kit.section("X1", "deferral") == ""
