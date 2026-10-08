from dataclasses import dataclass, field, replace

import pytest

from taskmanager.core.enums import NodeKind
from taskmanager.core.status import DecisionStatus, Merge, Status
from taskmanager.engine.chains import TOP
from taskmanager.engine.stepgraph import SnapNode, Snapshot
from taskmanager.engine.validation import Refusal, validate

SPEC, PLAN, TASK, DECISION = NodeKind.SPEC, NodeKind.PLAN, NodeKind.TASK, NodeKind.DECISION
PARENT = Merge.PARENT


@dataclass
class Branches:
    existing: set[str] = field(default_factory=set)
    cut_from: dict[str, str] = field(default_factory=dict)

    def branch_exists(self, node_id: str) -> bool:
        return node_id in self.existing

    def base_matches(self, node_id: str, new_target: str, new_top: str) -> bool:
        return self.cut_from.get(node_id) == new_target.removeprefix(TOP)


def snap(*nodes: SnapNode, edges: list[tuple[str, str]] | None = None) -> Snapshot:
    return Snapshot({n.id: n for n in nodes}, edges or [])


def with_node(s: Snapshot, n: SnapNode) -> Snapshot:
    return Snapshot({**s.nodes, n.id: n}, list(s.edges))


def rules(
    before: Snapshot, after: Snapshot, touched: set[str], branches: Branches | None = None
) -> list[tuple[str, int]]:
    found = validate(before, after, touched, branches or Branches())
    return [(r.node_id, r.rule) for r in found]


PLAN_P = SnapNode("P", PLAN)
EMPTY = snap(PLAN_P)


def test_a_valid_write_is_accepted() -> None:
    after = with_node(EMPTY, SnapNode("T", TASK, parent="P", merge=PARENT))
    assert validate(EMPTY, after, {"T"}, Branches()) == []


def test_fix_without_review_is_refused() -> None:
    after = with_node(EMPTY, SnapNode("T", TASK, review=False, fix=True))
    assert rules(EMPTY, after, {"T"}) == [("T", 1)]


@pytest.mark.parametrize(
    ("parent", "child", "refused"),
    [
        (SnapNode("P", PLAN), SnapNode("T", TASK, fix=False), True),
        (SnapNode("P", PLAN), SnapNode("T", TASK, parent="P", fix=False), True),
        (SnapNode("P", PLAN), SnapNode("T", TASK, parent="P", merge=PARENT, fix=False), False),
        (
            SnapNode("P", PLAN, review=False, fix=False),
            SnapNode("T", TASK, parent="P", merge=PARENT, fix=False),
            True,
        ),
        (SnapNode("P", PLAN), SnapNode("T", TASK, review=False, fix=False), False),
    ],
)
def test_review_without_fix_needs_a_parent_that_reviews_and_fixes_the_landing(
    parent: SnapNode, child: SnapNode, refused: bool
) -> None:
    before = snap(parent)
    after = with_node(before, child)
    assert rules(before, after, {"T"}) == ([("T", 2)] if refused else [])


def test_a_parent_write_that_strands_a_child_is_refused_on_the_child() -> None:
    child = SnapNode("T", TASK, parent="P", merge=PARENT, fix=False)
    before = snap(PLAN_P, child)
    after = with_node(before, replace(PLAN_P, review=False, fix=False))
    assert rules(before, after, {"P"}) == [("T", 2)]


@pytest.mark.parametrize(
    "node",
    [SnapNode("S", SPEC, merge=PARENT), SnapNode("T", TASK, merge=PARENT)],
)
def test_merge_parent_on_a_spec_or_a_parentless_node_is_refused(node: SnapNode) -> None:
    after = snap(node)
    assert rules(snap(), after, {node.id}) == [(node.id, 3)]


@pytest.mark.parametrize(
    ("branches", "refused"),
    [
        (Branches(), False),
        (Branches(existing={"T"}, cut_from={"T": "main"}), True),
        (Branches(existing={"T"}, cut_from={"T": "P"}), False),
    ],
)
def test_changing_where_a_node_lands_needs_its_branch_cut_from_the_new_target(
    branches: Branches, refused: bool
) -> None:
    before = snap(PLAN_P, SnapNode("T", TASK, parent="P"))
    after = with_node(before, SnapNode("T", TASK, parent="P", merge=PARENT))
    assert rules(before, after, {"T"}, branches) == ([("T", 4)] if refused else [])


@pytest.mark.parametrize(
    ("status", "steps"),
    [
        (Status.IMPLEMENTED, "defer it, then reopen it with --new-branch"),
        (Status.FAILED, "reopen it with --new-branch"),
        (Status.DEFERRED, "reopen it with --new-branch"),
        (
            Status.IMPLEMENTING,
            "wait for its step to end, or stop it, then defer it, then reopen it with --new-branch",
        ),
    ],
)
def test_a_refused_retarget_names_the_steps_that_set_the_branch_aside_from_its_status(
    status: Status, steps: str
) -> None:
    before = snap(PLAN_P, SnapNode("T", TASK, parent="P", status=status))
    after = with_node(before, SnapNode("T", TASK, parent="P", merge=PARENT, status=status))
    branches = Branches(existing={"T"}, cut_from={"T": "main"})
    [refusal] = validate(before, after, {"T"}, branches)
    assert f"{steps} before changing where it lands" in refusal.message
    assert refusal.message.count("defer") == steps.count("defer")


@pytest.mark.parametrize(
    ("status", "advice"),
    [
        (Status.COMPLETED, "its code has landed; file a new task to land on P"),
        (Status.SUPERSEDED, "its replacement carries the work; change where that lands"),
    ],
)
def test_a_refused_retarget_of_a_node_that_cannot_be_set_aside_names_no_defer(
    status: Status, advice: str
) -> None:
    before = snap(PLAN_P, SnapNode("T", TASK, parent="P", status=status))
    after = with_node(before, SnapNode("T", TASK, parent="P", merge=PARENT, status=status))
    branches = Branches(existing={"T"}, cut_from={"T": "main"})
    [refusal] = validate(before, after, {"T"}, branches)
    assert advice in refusal.message
    assert "defer" not in refusal.message


def test_moving_a_node_whose_branch_exists_to_another_parent_branch_is_refused() -> None:
    q = SnapNode("Q", PLAN)
    before = snap(PLAN_P, q, SnapNode("T", TASK, parent="P", merge=PARENT))
    after = with_node(before, SnapNode("T", TASK, parent="Q", merge=PARENT))
    branches = Branches(existing={"T"}, cut_from={"T": "P"})
    assert rules(before, after, {"T"}, branches) == [("T", 4)]


def test_a_merge_change_that_keeps_the_target_is_not_a_retarget() -> None:
    before = snap(SnapNode("T", TASK))
    after = snap(SnapNode("T", TASK, merge=PARENT))
    branches = Branches(existing={"T"}, cut_from={"T": "elsewhere"})
    assert rules(before, after, {"T"}, branches) == [("T", 3)]


@pytest.mark.parametrize(
    ("merge", "top", "refused"),
    [(PARENT, "main", True), (Merge.SPEC, "main", False), (Merge.SPEC, "release/2", True)],
)
def test_a_literal_origin_main_verification_is_refused_unless_the_node_lands_on_main(
    merge: Merge, top: str, refused: bool
) -> None:
    node = SnapNode("T", TASK, parent="P", merge=merge, literal_origin_main=True, top=top)
    assert rules(EMPTY, with_node(EMPTY, node), {"T"}) == ([("T", 5)] if refused else [])


def test_a_literal_origin_main_refusal_names_the_target_the_node_lands_on() -> None:
    node = SnapNode("T", TASK, parent="P", literal_origin_main=True, top="release/2")
    [refusal] = validate(EMPTY, with_node(EMPTY, node), {"T"}, Branches())
    assert refusal.message.startswith("T: lands on its target release/2 but a test_command")


@pytest.mark.parametrize(
    ("node", "refused"),
    [
        (SnapNode("S", SPEC, land_on="release/2"), False),
        (SnapNode("S", SPEC, land_on="a..b"), True),
        (SnapNode("S", SPEC, land_on=""), True),
        (SnapNode("P", PLAN, land_on="release/2"), True),
        (SnapNode("T", TASK, land_on="release/2"), True),
    ],
    ids=["spec", "invalid-name", "empty", "on-a-plan", "on-a-task"],
)
def test_land_on_is_refused_off_a_spec_or_when_git_rejects_the_name(
    node: SnapNode, refused: bool
) -> None:
    assert rules(snap(), snap(node), {node.id}) == ([(node.id, 12)] if refused else [])


def test_a_changed_land_on_rechecks_every_cut_branch_landing_at_the_top_below_the_spec() -> None:
    nodes = (
        SnapNode("S", SPEC),
        SnapNode("P", PLAN, parent="S"),
        SnapNode("T", TASK, parent="P"),
        SnapNode("C", TASK, parent="P", merge=PARENT),
    )
    before = snap(*nodes)
    after = snap(*(replace(n, top="release/2") for n in nodes))
    branches = Branches(existing={"T", "C"}, cut_from={"T": "main", "C": "P"})
    [refusal] = validate(before, after, {"S"}, branches)
    assert (refusal.node_id, refusal.rule) == ("T", 4)
    assert refusal.message.startswith("T: its branch exists and was not cut from release/2")
    assert rules(before, after, {"S"}, replace(branches, cut_from={"T": "release/2"})) == []


COMPLETED_P = SnapNode("P", PLAN, status=Status.COMPLETED)
BUSY_P = SnapNode("P", PLAN, status=Status.REVIEWING, busy=True)
HOLDERS = [
    COMPLETED_P,
    BUSY_P,
    # A step whose agent died still owns the container until a sweep returns it.
    SnapNode("P", PLAN, status=Status.MERGING),
    SnapNode("P", PLAN, status=Status.DEFERRED),
    SnapNode("P", PLAN, status=Status.ABANDONED),
    SnapNode("P", PLAN, status=Status.FAILED),
]


@pytest.mark.parametrize(
    "holder", HOLDERS, ids=["completed", "busy", "stale-step", "deferred", "abandoned", "failed"]
)
@pytest.mark.parametrize(
    ("child_before", "child_after"),
    [
        (None, SnapNode("T", TASK, parent="P")),
        (SnapNode("T", TASK, parent="Q"), SnapNode("T", TASK, parent="P")),
        (
            SnapNode("T", TASK, parent="P", status=Status.FAILED),
            SnapNode("T", TASK, parent="P"),
        ),
        (
            SnapNode("T", TASK, parent="P", status=Status.DEFERRED),
            SnapNode("T", TASK, parent="P"),
        ),
        (
            SnapNode("T", TASK, parent="P", status=Status.COMPLETED),
            SnapNode("T", TASK, parent="P"),
        ),
    ],
    ids=["new", "moved-in", "reopened-failed", "reopened-deferred", "reset-out-of-completed"],
)
def test_nothing_arrives_under_a_completed_or_busy_container(
    holder: SnapNode, child_before: SnapNode | None, child_after: SnapNode
) -> None:
    before = snap(holder, SnapNode("Q", PLAN))
    if child_before is not None:
        before = with_node(before, child_before)
    after = with_node(before, child_after)
    assert rules(before, after, {"T"}) == [("T", 6)]


@pytest.mark.parametrize("exit_", [Status.DEFERRED, Status.ABANDONED, Status.FAILED])
def test_a_child_arriving_under_a_set_aside_container_is_told_to_reopen_it(
    exit_: Status,
) -> None:
    before = snap(SnapNode("P", PLAN, status=exit_))
    after = with_node(before, SnapNode("T", TASK, parent="P"))
    [refusal] = validate(before, after, {"T"}, Branches())
    assert "reopen P first" in refusal.message


def test_nothing_arrives_under_a_container_whose_ancestor_completed() -> None:
    before = snap(
        SnapNode("S", SPEC, status=Status.COMPLETED),
        SnapNode("P", PLAN, parent="S"),
    )
    after = with_node(before, SnapNode("T", TASK, parent="P"))
    assert rules(before, after, {"T"}) == [("T", 6)]


def test_a_completed_container_arriving_with_its_children_in_one_write_is_accepted() -> None:
    before = snap()
    after = snap(
        COMPLETED_P,
        SnapNode("T", TASK, parent="P", status=Status.COMPLETED),
    )
    assert rules(before, after, {"P", "T"}) == []


def test_a_child_already_under_a_completed_container_may_be_edited() -> None:
    before = snap(COMPLETED_P, SnapNode("T", TASK, parent="P", status=Status.COMPLETED))
    after = with_node(before, SnapNode("T", TASK, parent="P", status=Status.COMPLETED, repo="x"))
    assert rules(before, after, {"T"}) == []


BUSY_T = SnapNode("T", TASK, status=Status.IMPLEMENTING, busy=True)


@pytest.mark.parametrize(
    ("after_node", "after_edges"),
    [
        (replace(BUSY_T, status=Status.DEFERRED), [("T", "X")]),
        (replace(BUSY_T, status=Status.READY), [("T", "X")]),
        (replace(BUSY_T, parent="P"), [("T", "X")]),
        (BUSY_T, [("T", "X"), ("T", "D")]),
        (BUSY_T, []),
    ],
    ids=["deferred", "reset", "moved", "decision-linked", "edge-dropped"],
)
def test_a_busy_node_refuses_every_change_of_state_place_or_waits(
    after_node: SnapNode, after_edges: list[tuple[str, str]]
) -> None:
    others = [PLAN_P, SnapNode("X", TASK), SnapNode("D", DECISION, status=DecisionStatus.OPEN)]
    before = snap(BUSY_T, *others, edges=[("T", "X")])
    after = snap(after_node, *others, edges=after_edges)
    assert rules(before, after, {"T"}) == [("T", 7)]


def test_a_busy_node_may_gain_a_dependency_for_its_next_claim() -> None:
    before = snap(BUSY_T, SnapNode("X", TASK))
    after = snap(BUSY_T, SnapNode("X", TASK), edges=[("T", "X")])
    assert rules(before, after, {"T"}) == []


def test_a_write_that_closes_a_cycle_is_refused_with_the_path() -> None:
    before = snap(PLAN_P, SnapNode("A", TASK, parent="P", merge=PARENT))
    after = snap(PLAN_P, SnapNode("A", TASK, parent="P", merge=PARENT), edges=[("A", "P")])
    refusals = validate(before, after, {"A"}, Branches())
    assert refusals == [
        Refusal(
            "A",
            8,
            "this write makes the step graph cyclic: A.start ← P.landed ← P.implemented ← "
            "A.landed ← A.implemented ← A.start; remove an edge on the cycle or change where "
            "a node on it lands",
        )
    ]


def test_an_untouched_node_is_not_checked() -> None:
    broken = SnapNode("Z", TASK, review=False, fix=True)
    before = snap(PLAN_P, broken)
    after = with_node(before, SnapNode("T", TASK))
    assert rules(before, after, {"T"}) == []


def test_a_decision_is_not_held_to_node_flag_rules() -> None:
    after = snap(SnapNode("D", DECISION, status=DecisionStatus.OPEN, review=False))
    assert rules(snap(), after, {"D"}) == []


@pytest.mark.parametrize(
    ("status", "claimed_from", "review", "refused"),
    [
        (Status.LANDED, None, False, True),
        (Status.LANDED, None, True, False),
        (Status.COMPLETED, None, False, False),
        (Status.REVIEWING, Status.LANDED, False, True),
        (Status.REVIEWING, Status.LANDED, True, False),
        (Status.REVIEWING, Status.IMPLEMENTED, False, False),
    ],
)
def test_review_off_is_refused_only_on_a_node_at_landed_or_in_a_step_claimed_from_it(
    status: Status, claimed_from: Status | None, review: bool, refused: bool
) -> None:
    before = snap(SnapNode("P", PLAN, status=status, claimed_from=claimed_from))
    after = snap(
        SnapNode("P", PLAN, status=status, claimed_from=claimed_from, review=review, fix=review)
    )
    assert rules(before, after, {"P"}) == ([("P", 11)] if refused else [])
