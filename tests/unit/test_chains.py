from dataclasses import dataclass, field

import pytest

from taskmanager.core.status import DecisionStatus, Merge, Status
from taskmanager.engine.chains import (
    base_chain,
    landing_chain,
    landing_target,
    meeting,
    satisfied,
    sync_pairs,
)

PARENT = Merge.PARENT
TOP_MAIN = "TOP:main"


@dataclass
class Tree:
    """S is a spec; P and Q are its plans; T1, T2, G sit under P and U under Q; M is a
    top-level task; D is a decision. P, T1, T2 and U land on their parent's branch."""

    statuses: dict[str, Status | DecisionStatus] = field(default_factory=dict)
    rows: dict[str, tuple[str | None, Merge]] = field(
        default_factory=lambda: {
            "S": (None, Merge.SPEC),
            "P": ("S", PARENT),
            "Q": ("S", Merge.SPEC),
            "T1": ("P", PARENT),
            "T2": ("P", PARENT),
            "G": ("P", Merge.SPEC),
            "U": ("Q", PARENT),
            "M": (None, Merge.SPEC),
            "D": (None, Merge.SPEC),
            "ORPHAN": (None, PARENT),
        }
    )

    def parent(self, node_id: str) -> str | None:
        return self.rows[node_id][0]

    def merge(self, node_id: str) -> Merge:
        return self.rows[node_id][1]

    def status(self, node_id: str) -> Status | DecisionStatus:
        return self.statuses.get(node_id, Status.READY)

    def top(self, node_id: str) -> str:
        return "main"


@pytest.mark.parametrize(
    ("node", "target"),
    [
        ("S", TOP_MAIN),
        ("P", "S"),
        ("Q", TOP_MAIN),
        ("T1", "P"),
        ("G", TOP_MAIN),
        ("U", "Q"),
        ("ORPHAN", TOP_MAIN),
    ],
)
def test_landing_target_is_the_parent_only_for_merge_parent(node: str, target: str) -> None:
    assert landing_target(Tree(), node) == target


@pytest.mark.parametrize(
    ("node", "chain"),
    [
        ("S", [TOP_MAIN]),
        ("P", ["S", TOP_MAIN]),
        ("T1", ["P", "S", TOP_MAIN]),
        ("G", [TOP_MAIN]),
        ("U", ["Q", TOP_MAIN]),
        ("M", [TOP_MAIN]),
    ],
)
def test_base_chain_lists_the_branches_a_node_builds_on_nearest_first(
    node: str, chain: list[str]
) -> None:
    assert base_chain(Tree(), node) == chain


@pytest.mark.parametrize(
    ("node", "chain"),
    [
        ("S", ["S"]),
        ("P", ["P", "S"]),
        ("T1", ["T1", "P", "S"]),
        ("G", ["G"]),
        ("U", ["U", "Q"]),
        ("M", ["M"]),
    ],
)
def test_landing_chain_follows_the_code_up_to_the_node_that_lands_on_its_target(
    node: str, chain: list[str]
) -> None:
    assert landing_chain(Tree(), node) == chain


@pytest.mark.parametrize(
    ("x", "y", "z", "pairs"),
    [
        ("T2", "T1", "T1", []),
        ("T1", "M", "M", [(TOP_MAIN, "S"), ("S", "P")]),
        ("P", "M", "M", [(TOP_MAIN, "S")]),
        ("M", "T1", "S", []),
        ("G", "T1", "S", []),
        ("U", "T1", "S", [(TOP_MAIN, "Q")]),
        ("T1", "G", "G", [(TOP_MAIN, "S"), ("S", "P")]),
        ("T1", "U", "Q", [(TOP_MAIN, "S"), ("S", "P")]),
        ("Q", "P", "S", []),
    ],
)
def test_a_dependency_meets_the_dependent_where_its_code_reaches_a_base_branch(
    x: str, y: str, z: str, pairs: list[tuple[str, str]]
) -> None:
    tree = Tree()
    assert meeting(tree, x, y) == z
    assert sync_pairs(tree, x, y) == pairs


@pytest.mark.parametrize(
    ("x", "y", "statuses", "expected"),
    [
        ("T2", "T1", {"T1": Status.COMPLETED}, True),
        ("T2", "T1", {"T1": Status.MERGING}, False),
        ("M", "T1", {"T1": Status.COMPLETED, "P": Status.COMPLETED}, False),
        ("M", "T1", {"T1": Status.COMPLETED, "P": Status.COMPLETED, "S": Status.COMPLETED}, True),
        ("G", "T1", {"T1": Status.COMPLETED, "P": Status.COMPLETED}, False),
        ("T1", "M", {"M": Status.COMPLETED}, True),
        ("T1", "M", {"M": Status.REVIEWED}, False),
        ("U", "T1", {"S": Status.COMPLETED}, True),
        ("M", "T1", {"T1": Status.COMPLETED, "P": Status.COMPLETED, "S": Status.LANDED}, True),
        ("U", "T1", {"S": Status.LANDED}, True),
        ("U", "T1", {"S": Status.MERGING}, False),
        ("T2", "T1", {"T1": Status.SUPERSEDED}, True),
        ("M", "T1", {"T1": Status.SUPERSEDED}, True),
        ("T2", "T1", {"T1": Status.ABANDONED}, False),
        ("T2", "T1", {"T1": Status.FAILED}, False),
        ("M", "D", {"D": DecisionStatus.OPEN}, False),
        ("M", "D", {"D": DecisionStatus.ANSWERED}, True),
        ("M", "D", {"D": DecisionStatus.WITHDRAWN}, True),
    ],
)
def test_an_edge_is_satisfied_once_the_meeting_node_has_landed(
    x: str, y: str, statuses: dict[str, Status | DecisionStatus], expected: bool
) -> None:
    assert satisfied(Tree(statuses), x, y) is expected
