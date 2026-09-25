import random

import pytest

from taskmanager.core.enums import NodeKind
from taskmanager.core.status import SET_ASIDE, DecisionStatus, Merge, Status
from taskmanager.engine.chains import meeting
from taskmanager.engine.stepgraph import (
    SnapNode,
    Snapshot,
    find_cycle,
    format_cycle,
    migration_order,
)

SPEC, PLAN, TASK, DECISION = NodeKind.SPEC, NodeKind.PLAN, NodeKind.TASK, NodeKind.DECISION
PARENT = Merge.PARENT


def snap(*nodes: SnapNode, edges: list[tuple[str, str]] | None = None) -> Snapshot:
    return Snapshot({n.id: n for n in nodes}, edges or [])


def test_snapshot_walks_the_tree_and_inherits_ancestor_edges() -> None:
    s = snap(
        SnapNode("S", SPEC),
        SnapNode("P", PLAN, parent="S", merge=PARENT),
        SnapNode("T2", TASK, parent="P"),
        SnapNode("T1", TASK, parent="P"),
        SnapNode("X", TASK),
        SnapNode("Y", TASK),
        edges=[("T1", "X"), ("S", "Y"), ("P", "X")],
    )
    assert s.children("P") == ["T1", "T2"]
    assert s.descendants("S") == ["P", "T1", "T2"]
    assert s.inherited_edges("T1") == ["X", "Y"]
    assert s.inherited_edges("T2") == ["X", "Y"]
    assert s.inherited_edges("X") == []


def test_a_dependency_on_the_own_container_prints_the_cycle_as_a_waits_on_path() -> None:
    s = snap(
        SnapNode("P", PLAN),
        SnapNode("A", TASK, parent="P", merge=PARENT),
        edges=[("A", "P")],
    )
    assert format_cycle(find_cycle(s) or []) == (
        "A.start ← P.landed ← P.implemented ← A.landed ← A.implemented ← A.start"
    )


def test_an_acyclic_graph_has_no_cycle() -> None:
    s = snap(
        SnapNode("P", PLAN),
        SnapNode("A", TASK, parent="P", merge=PARENT),
        SnapNode("B", TASK, parent="P", merge=PARENT),
        SnapNode("M", TASK),
        edges=[("B", "A"), ("A", "M")],
    )
    assert find_cycle(s) is None


def test_an_edge_on_a_container_gates_its_descendants_and_can_close_a_cycle() -> None:
    s = snap(
        SnapNode("P", PLAN),
        SnapNode("D", TASK, parent="P"),
        SnapNode("Y", TASK),
        edges=[("P", "Y"), ("Y", "D")],
    )
    cycle = find_cycle(s)
    assert cycle is not None
    assert {"D.start", "Y.landed", "Y.start", "D.landed"} <= set(cycle)


def test_a_superseded_dependency_waits_on_nothing() -> None:
    s = snap(
        SnapNode("A", TASK),
        SnapNode("B", TASK, status=Status.SUPERSEDED),
        edges=[("A", "B"), ("B", "A")],
    )
    assert find_cycle(s) is None


def test_a_set_aside_child_does_not_hold_its_container() -> None:
    s = snap(
        SnapNode("P", PLAN),
        SnapNode("K", TASK, parent="P", merge=PARENT, status=Status.DEFERRED),
        edges=[("K", "P")],
    )
    assert find_cycle(s) is None


def test_edges_to_decisions_are_left_out() -> None:
    s = snap(
        SnapNode("A", TASK),
        SnapNode("Q", DECISION, status=DecisionStatus.OPEN),
        edges=[("A", "Q")],
    )
    assert find_cycle(s) is None


def _migration_estate(a_status: Status) -> Snapshot:
    # A writes a migration on tm/P; B writes one straight to main; C, also on tm/P, needs B.
    return snap(
        SnapNode("P", PLAN, repo="core"),
        SnapNode(
            "A", TASK, parent="P", merge=PARENT, repo="core", writes_migration=True, status=a_status
        ),
        SnapNode("C", TASK, parent="P", merge=PARENT, repo="core"),
        SnapNode("B", TASK, repo="core", writes_migration=True),
        edges=[("C", "B")],
    )


def test_the_migration_chain_is_granted_first_to_the_writer_whose_code_reaches_main_first() -> None:
    s = _migration_estate(Status.READY)
    assert migration_order(s, "core") == ["B", "A"]
    assert find_cycle(s) is None


def test_a_chain_held_across_a_container_boundary_closes_a_cycle() -> None:
    cycle = find_cycle(_migration_estate(Status.IMPLEMENTING))
    assert cycle is not None
    assert {"B.start", "P.landed", "C.start", "B.landed"} <= set(cycle)


def test_sibling_writers_take_the_chain_in_dependency_order() -> None:
    s = snap(
        SnapNode("P", PLAN, repo="core"),
        SnapNode("A1", TASK, parent="P", merge=PARENT, repo="core", writes_migration=True),
        SnapNode("A2", TASK, parent="P", merge=PARENT, repo="core", writes_migration=True),
        edges=[("A1", "A2")],
    )
    assert migration_order(s, "core") == ["A2", "A1"]
    assert find_cycle(s) is None


def test_a_writer_whose_code_is_on_main_no_longer_holds_the_chain() -> None:
    s = snap(
        SnapNode("A", TASK, repo="core", writes_migration=True, status=Status.COMPLETED),
        SnapNode("B", TASK, repo="core", writes_migration=True),
        SnapNode("W", TASK, repo="web", writes_migration=True),
    )
    assert migration_order(s, "core") == ["B"]


def _random_snapshot(rng: random.Random) -> Snapshot:
    nodes: dict[str, SnapNode] = {}
    for i in range(rng.randint(1, 8)):
        kind = rng.choice([SPEC, PLAN, PLAN, TASK, TASK, TASK, DECISION])
        containers = [n.id for n in nodes.values() if n.kind in (SPEC, PLAN)]
        parent = None
        if kind in (PLAN, TASK) and containers and rng.random() < 0.7:
            parent = rng.choice(containers)
        merge = PARENT if parent is not None and rng.random() < 0.6 else Merge.MAIN
        status: Status | DecisionStatus = rng.choice(
            [Status.READY] * 4
            + [Status.COMPLETED, Status.IMPLEMENTING, Status.SUPERSEDED, Status.DEFERRED]
        )
        if kind == DECISION:
            status = DecisionStatus.OPEN
        nodes[f"N{i}"] = SnapNode(f"N{i}", kind, parent=parent, merge=merge, status=status)
    ids = list(nodes)
    edges = [
        (x, y)
        for x, y in (tuple(rng.sample(ids, 2)) for _ in range(rng.randint(0, 6)) if len(ids) > 1)
    ]
    return Snapshot(nodes, edges)


def _oracle_edges(s: Snapshot) -> set[tuple[str, str]]:
    work = {i for i, n in s.nodes.items() if n.kind != DECISION}
    edges: set[tuple[str, str]] = set()
    for i in work:
        edges |= {(f"{i}.start", f"{i}.implemented"), (f"{i}.implemented", f"{i}.landed")}
        parent = s.nodes[i].parent
        if parent in work and s.nodes[i].status not in SET_ASIDE:
            edges.add((f"{i}.landed", f"{parent}.implemented"))

    def under(d: str, x: str) -> bool:
        node: str | None = d
        while node is not None:
            if node == x:
                return True
            node = s.nodes[node].parent
        return False

    for x, y in s.edges:
        if x in work and y in work and s.nodes[y].status != Status.SUPERSEDED:
            for d in work:
                if under(d, x):
                    edges.add((f"{meeting(s, d, y)}.landed", f"{d}.start"))
    return edges


def _oracle_has_cycle(edges: set[tuple[str, str]]) -> bool:
    successors: dict[str, set[str]] = {}
    for a, b in edges:
        successors.setdefault(a, set()).add(b)
    for vertex, direct in successors.items():
        seen: set[str] = set()
        frontier = list(direct)
        while frontier:
            current = frontier.pop()
            if current == vertex:
                return True
            if current not in seen:
                seen.add(current)
                frontier.extend(successors.get(current, ()))
    return False


@pytest.mark.parametrize("seed", range(500))
def test_a_cycle_is_found_exactly_when_one_exists_and_the_path_is_real(seed: int) -> None:
    s = _random_snapshot(random.Random(seed))
    edges = _oracle_edges(s)
    path = find_cycle(s)
    assert (path is not None) == _oracle_has_cycle(edges)
    if path is not None:
        assert path[0] == path[-1] and len(path) > 2
        assert all((path[i + 1], path[i]) in edges for i in range(len(path) - 1))
