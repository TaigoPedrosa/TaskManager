"""The wave simulator: `depth` waves ahead of a live snapshot, each node's step assumed to
succeed. Wave 1 is `selection.candidates` + `selection.select` on the snapshot as given, plus
every node already mid-step (in flight); each later wave reads a snapshot where the previous
wave's nodes, chosen or in flight, landed their step's best outcome -- complete, approve, land.
Transitions come from `core.lifecycle`, rollup from `core.rollup`; nothing here re-derives either.

A simulated wave has no real claim to run a condition's command under, so `cached_conditions` (the
same condition-result cache a display reads once per view) is the only signal it can ever fold in;
`repo_order` is threaded through so a container's `repos` matches a real claim's.
"""

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field, replace
from functools import partial

from taskmanager.core.enums import CONTAINERS, NodeKind
from taskmanager.core.lifecycle import Caps, advance, claim, fix_round
from taskmanager.core.models import Node
from taskmanager.core.rollup import rollup
from taskmanager.core.status import IN_STEP, Action, Event, Status
from taskmanager.engine import selection
from taskmanager.engine.config import ModelsConfig
from taskmanager.engine.routing import model_for
from taskmanager.engine.snapshot import apply_cycle, cycle_in
from taskmanager.engine.stepgraph import SnapNode, Snapshot

_EVENT_OF: dict[Action, Event] = {
    Action.IMPLEMENT: Event.COMPLETE,
    Action.FIX: Event.COMPLETE,
    Action.REVIEW: Event.APPROVE,
    Action.MERGE: Event.LANDED,
}
_ACTION_OF_STATUS: dict[Status, Action] = {
    Status.IMPLEMENTING: Action.IMPLEMENT,
    Status.REVIEWING: Action.REVIEW,
    Status.FIXING: Action.FIX,
    Status.MERGING: Action.MERGE,
}


@dataclass(frozen=True)
class WaveEntry:
    id: str
    title: str
    kind: NodeKind
    action: Action
    model: str
    repos: list[str]
    status_before: Status
    status_after: Status
    in_flight: bool = False


@dataclass(frozen=True)
class Wave:
    entries: list[WaveEntry] = field(default_factory=list)
    held: list[str] = field(default_factory=list)


def simulate(
    snapshot: Snapshot,
    depth: int,
    size: int,
    max_strong: int,
    specs: list[str] | None,
    *,
    repo_order: Sequence[str] = (),
    cached_conditions: Mapping[tuple[str, int], tuple[str, int]] | None = None,
    gated: Collection[str] | None = None,
    models: ModelsConfig | None = None,
) -> list[Wave]:
    """`depth` waves out from `snapshot`, each a `select` over the snapshot the wave before it
    left: `snapshot` itself is read only, never written, and this issues no SQL -- every wave
    after the first reads a snapshot this function built in memory, not the database."""
    caps = Caps()
    models = models or ModelsConfig()
    snap = snapshot
    waves: list[Wave] = []
    for _ in range(depth):
        snap, wave = _advance(
            snap, size, max_strong, specs, caps, repo_order, cached_conditions, gated, models
        )
        waves.append(wave)
    return waves


def _advance(
    snap: Snapshot,
    size: int,
    max_strong: int,
    specs: list[str] | None,
    caps: Caps,
    repo_order: Sequence[str],
    cached_conditions: Mapping[tuple[str, int], tuple[str, int]] | None,
    gated: Collection[str] | None,
    models: ModelsConfig,
) -> tuple[Snapshot, Wave]:
    data = snap.graph_data()
    blocked_reason = partial(
        selection.blocked_reason, repo_order=repo_order, cached_conditions=cached_conditions
    )
    found, candidate_held = selection.candidates(
        snap,
        specs,
        repo_order=repo_order,
        models=models,
        blocked_reason=blocked_reason,
        gated=gated,
    )
    result = selection.select(found, snap, size, max_strong, strong=models.strong)
    cand_by_id = {c.node.id: c for c in found}

    new_nodes = dict(snap.nodes)
    new_data_nodes = dict(data.nodes)
    new_leases = dict(data.leases)
    new_jobs = {node_id: list(jobs) for node_id, jobs in data.jobs.items()}
    changed: set[str] = set()
    entries: list[WaveEntry] = []

    def settle(node: Node, action: Action) -> tuple[Status, Status]:
        before = node.status
        if not isinstance(before, Status):
            raise TypeError(f"{node.id}: a decision never runs a step")
        new_leases.pop(node.id, None)
        new_jobs.pop(node.id, None)
        if action == Action.SYNC:
            # A sync carries a branch up to date; it never moves the node's own status.
            return before, before
        cycle = cycle_in(snap, node)
        if before not in IN_STEP:
            cycle = claim(cycle)
        advanced = advance(cycle, _EVENT_OF[action], caps)
        new_data_nodes[node.id] = apply_cycle(node, advanced)
        new_nodes[node.id] = replace(new_nodes[node.id], status=advanced.status)
        changed.add(node.id)
        return before, advanced.status

    for entry in result.chosen:
        cand = cand_by_id[str(entry["id"])]
        before, after = settle(cand.node, cand.action)
        entries.append(
            WaveEntry(
                id=cand.node.id,
                title=cand.node.title,
                kind=cand.node.kind,
                action=cand.action,
                model=cand.model,
                repos=cand.repos,
                status_before=before,
                status_after=after,
            )
        )

    in_flight_ids = sorted(
        node.id
        for node in data.nodes.values()
        if node.kind != NodeKind.DECISION
        and node.id not in cand_by_id
        and isinstance(node.status, Status)
        and node.status in IN_STEP
        and selection._in_scope(snap, node, specs)
    )
    for node_id in in_flight_ids:
        node = data.nodes[node_id]
        action = _ACTION_OF_STATUS[Status(node.status)]
        model = model_for(action, node, fix_round(cycle_in(snap, node)), models)
        before, after = settle(node, action)
        entries.append(
            WaveEntry(
                id=node.id,
                title=node.title,
                kind=node.kind,
                action=action,
                model=model,
                repos=selection.repos_of(snap, node_id, repo_order),
                status_before=before,
                status_after=after,
                in_flight=True,
            )
        )

    _roll_up(new_nodes, new_data_nodes, snap, changed)
    new_data = replace(data, nodes=new_data_nodes, leases=new_leases, jobs=new_jobs)
    new_snap = Snapshot(nodes=new_nodes, edges=snap.edges, data=new_data, config=snap.config)
    return new_snap, Wave(entries=entries, held=[*candidate_held, *result.held])


def _roll_up(
    nodes: dict[str, SnapNode], data_nodes: dict[str, Node], snap: Snapshot, changed: set[str]
) -> None:
    """Every ancestor container above a status this wave changed, re-derived from its own
    children -- one level at a time, exactly as `rollup` decides, walked all the way to the root
    regardless of whether a given level actually moved."""
    seen: set[str] = set()
    queue = list(changed)
    while queue:
        parent_id = snap.parent(queue.pop(0))
        if parent_id is None or parent_id in seen:
            continue
        seen.add(parent_id)
        parent = nodes[parent_id]
        if parent.kind in CONTAINERS and isinstance(parent.status, Status):
            children: list[Status] = [
                status
                for c in snap.children(parent_id)
                if isinstance(status := nodes[c].status, Status)
            ]
            derived = rollup(parent.status, children)
            if derived != parent.status:
                nodes[parent_id] = replace(parent, status=derived)
                data_nodes[parent_id] = apply_cycle(
                    data_nodes[parent_id],
                    replace(cycle_in(snap, data_nodes[parent_id]), status=derived),
                )
        queue.append(parent_id)
