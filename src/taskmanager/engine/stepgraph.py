"""The expanded step graph: every node as start -> implemented -> landed, with an edge wherever
one step waits on another. It is the only deadlock guard, so every graph-changing write is
refused when it closes a cycle here."""

from dataclasses import dataclass, field
from graphlib import CycleError, TopologicalSorter

from taskmanager.core.enums import CONTAINERS, NodeKind
from taskmanager.core.status import EXITS, SET_ASIDE, DecisionStatus, Merge, Status
from taskmanager.db.graph_reader import GraphData
from taskmanager.engine.chains import landing_chain, meeting, satisfied

Graph = dict[str, set[str]]


@dataclass(frozen=True)
class SnapNode:
    id: str
    kind: NodeKind
    parent: str | None = None
    merge: Merge = Merge.MAIN
    status: Status | DecisionStatus = Status.READY
    review: bool = True
    fix: bool = True
    repo: str | None = None
    writes_migration: bool = False
    busy: bool = False
    literal_origin_main: bool = False


@dataclass
class Snapshot:
    nodes: dict[str, SnapNode]
    edges: list[tuple[str, str]]
    # The bulk read `SnapshotBuilder.build()` made this snapshot from: None only for a snapshot a
    # test builds by hand for the validation rules, which never read it.
    data: GraphData | None = None
    _children: dict[str, list[str]] = field(
        init=False, repr=False, compare=False, default_factory=dict
    )
    _edges_by_source: dict[str, list[str]] = field(
        init=False, repr=False, compare=False, default_factory=dict
    )

    def __post_init__(self) -> None:
        for node_id in sorted(self.nodes):
            parent = self.nodes[node_id].parent
            if parent is not None:
                self._children.setdefault(parent, []).append(node_id)
        for dependent, dependency in self.edges:
            self._edges_by_source.setdefault(dependent, []).append(dependency)

    def parent(self, node_id: str) -> str | None:
        return self.nodes[node_id].parent

    def merge(self, node_id: str) -> Merge:
        return self.nodes[node_id].merge

    def status(self, node_id: str) -> Status | DecisionStatus:
        return self.nodes[node_id].status

    def children(self, node_id: str) -> list[str]:
        return list(self._children.get(node_id, ()))

    def descendants(self, node_id: str) -> list[str]:
        found: list[str] = []
        for child in self.children(node_id):
            found.append(child)
            found.extend(self.descendants(child))
        return found

    def counted_descendants(self, node_id: str) -> list[str]:
        """Descendants a container still counts: a set-aside node never lands, so it and its
        subtree are left out."""
        found: list[str] = []
        for child in self.children(node_id):
            if self.status(child) not in SET_ASIDE:
                found.append(child)
                found.extend(self.counted_descendants(child))
        return found

    def inherited_edges(self, node_id: str) -> list[str]:
        """Every dependency of `node_id` and of each of its ancestors, own first."""
        return list(self.edge_owners(node_id))

    def edge_owners(self, node_id: str) -> dict[str, str]:
        """`inherited_edges`, each mapped to the node (itself or an ancestor) that declares it."""
        found: dict[str, str] = {}
        owner: str | None = node_id
        while owner is not None:
            for dep in self._edges_by_source.get(owner, ()):
                found.setdefault(dep, owner)
            owner = self.parent(owner)
        return found


def _is_work(s: Snapshot, node_id: str) -> bool:
    return node_id in s.nodes and s.nodes[node_id].kind != NodeKind.DECISION


def _base_graph(s: Snapshot) -> Graph:
    graph: Graph = {}
    work = [n for n in s.nodes.values() if n.kind != NodeKind.DECISION]
    for n in work:
        graph[f"{n.id}.start"] = {f"{n.id}.implemented"}
        graph[f"{n.id}.implemented"] = {f"{n.id}.landed"}
        graph[f"{n.id}.landed"] = set()
    for n in work:
        if n.parent is not None and _is_work(s, n.parent) and n.status not in SET_ASIDE:
            graph[f"{n.id}.landed"].add(f"{n.parent}.implemented")
    for dependent, dependency in s.edges:
        if not (_is_work(s, dependent) and _is_work(s, dependency)):
            continue
        if s.status(dependency) == Status.SUPERSEDED:
            continue
        # An edge on a container gates every descendant's claim, each at the point where the
        # dependency's code reaches a branch that descendant builds on.
        for d in [dependent, *s.descendants(dependent)]:
            if _is_work(s, d):
                graph[f"{meeting(s, d, dependency)}.landed"].add(f"{d}.start")
    return graph


def _topological_rank(graph: Graph) -> dict[str, int]:
    predecessors: dict[str, set[str]] = {vertex: set() for vertex in graph}
    for vertex in sorted(graph):
        for successor in graph[vertex]:
            predecessors[successor].add(vertex)
    try:
        return {v: i for i, v in enumerate(TopologicalSorter(predecessors).static_order())}
    except CycleError:
        return {}


def _migration_order(s: Snapshot, repo: str, rank: dict[str, int]) -> list[str]:
    def reaches_main(node_id: str) -> str:
        return landing_chain(s, node_id)[-1]

    writers = [
        n
        for n in s.nodes.values()
        if n.repo == repo
        and n.writes_migration
        and n.kind not in CONTAINERS
        # Code parked on a branch that was set aside never reaches main, so it holds nothing.
        and not any(s.status(x) in EXITS for x in landing_chain(s, n.id))
        and s.status(reaches_main(n.id)) != Status.COMPLETED
    ]

    def key(n: SnapNode) -> tuple[int, int, int, str]:
        # Whoever already holds the chain keeps it: landed on a container branch first, then
        # in a step, then the rest in the order their code can reach main.
        held = 0 if n.status == Status.COMPLETED else 1 if n.status != Status.READY else 2
        main_rank = rank.get(f"{reaches_main(n.id)}.landed", 0)
        return held, main_rank, rank.get(f"{n.id}.start", 0), n.id

    return [n.id for n in sorted(writers, key=key)]


def migration_order(s: Snapshot, repo: str) -> list[str]:
    """The order `repo`'s migration chain is granted in: each writer waits until every earlier
    one's code reaches a branch it builds on. Discovery grants in this order, so the cycle
    check sees the same waits discovery will impose."""
    return _migration_order(s, repo, _topological_rank(_base_graph(s)))


def migration_holders(s: Snapshot, repo: str) -> dict[str, str]:
    """For every migration writer in `repo` still open, the earlier one (by `migration_order`)
    that holds its chain: the writer nearest ahead of it whose landing has not yet reached a
    branch it builds on. A writer already satisfied against the current holder carries the chain
    forward in its place, so a sibling building on already-landed work never waits behind an
    unrelated writer that merely ranks between them."""
    holders: dict[str, str] = {}
    current: str | None = None
    for entry in migration_order(s, repo):
        if current is not None and not satisfied(s, entry, current):
            holders[entry] = current
        else:
            current = entry
    return holders


def _with_migration_chain(s: Snapshot, graph: Graph) -> Graph:
    chained = {vertex: set(successors) for vertex, successors in graph.items()}
    rank = _topological_rank(graph)
    repos = sorted({n.repo for n in s.nodes.values() if n.writes_migration and n.repo})
    for repo in repos:
        order = _migration_order(s, repo, rank)
        for i, first in enumerate(order):
            for later in order[i + 1 :]:
                chained[f"{meeting(s, later, first)}.landed"].add(f"{later}.start")
    return chained


def _readable(loop: list[str]) -> list[str]:
    """`loop` runs along the edges; the result runs against them, so each vertex is followed by
    the one it waits on, starting at the smallest start vertex so one cycle prints one way."""
    waits = loop[::-1][:-1]
    first = min((v for v in waits if v.endswith(".start")), default=waits[0])
    i = waits.index(first)
    rotated = waits[i:] + waits[:i]
    return [*rotated, rotated[0]]


def _cycle(graph: Graph) -> list[str] | None:
    on_path: set[str] = set()
    done: set[str] = set()
    for root in sorted(graph):
        if root in done:
            continue
        path = [root]
        pending = [iter(sorted(graph[root]))]
        on_path.add(root)
        while pending:
            successor = next(pending[-1], None)
            if successor is None:
                finished = path.pop()
                on_path.discard(finished)
                done.add(finished)
                pending.pop()
            elif successor in on_path:
                return _readable([*path[path.index(successor) :], successor])
            elif successor not in done:
                path.append(successor)
                on_path.add(successor)
                pending.append(iter(sorted(graph.get(successor, ()))))
    return None


def find_cycle(s: Snapshot) -> list[str] | None:
    graph = _base_graph(s)
    return _cycle(graph) or _cycle(_with_migration_chain(s, graph))


def format_cycle(path: list[str]) -> str:
    return " ← ".join(path)
