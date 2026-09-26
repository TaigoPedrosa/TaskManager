"""Which node the graph would claim next, and which of those fit one wave: pure functions of a
`Snapshot`, so `Claims`, `tm wave discover` and the wave simulator choose exactly alike.

A condition needs a command run, which a snapshot alone cannot do, so `blocked_reason` here never
checks one; `Claims.blocked_reason` layers that check on top, over the same snapshot.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from taskmanager.core.enums import CONTAINERS, NodeKind
from taskmanager.core.lifecycle import LifecycleError, claim, fix_round, next_action
from taskmanager.core.models import Node
from taskmanager.core.status import IN_STEP, Action, DecisionStatus, JobKind, JobState, Status
from taskmanager.db.node_repo import declared_files_of, locked_key
from taskmanager.db.runtime_repo import lease_alive
from taskmanager.engine.chains import satisfied
from taskmanager.engine.routing import STRONG, model_for
from taskmanager.engine.snapshot import apply_cycle, cycle_of
from taskmanager.engine.stepgraph import Snapshot, migration_holders

# Later steps first, so a wave drains work already under way before it starts more.
_STAGE = {Action.MERGE: 0, Action.SYNC: 0, Action.FIX: 1, Action.REVIEW: 2, Action.IMPLEMENT: 3}
_LIVE_JOBS = frozenset({JobState.RUNNING, JobState.NEEDS_AGENT})


@dataclass(frozen=True)
class Candidate:
    node: Node
    action: Action
    model: str
    job: str | None
    repos: list[str]


@dataclass(frozen=True)
class Selection:
    chosen: list[dict[str, object]]
    held: list[str] = field(default_factory=list)
    waiting_for_slot: int = 0


def next_step(node: Node) -> tuple[Action | None, str | None]:
    """The action a claim would take now, and the model it would name, from the node's own
    stored cycle alone."""
    cycle = cycle_of(node)
    action = next_action(cycle)
    if action is None:
        return None, None
    claimed = claim(cycle)
    return action, model_for(action, apply_cycle(node, claimed), fix_round(claimed))


def repos_of(snap: Snapshot, node_id: str, repo_order: Sequence[str] = ()) -> list[str]:
    """A task's target repository; a container's, the repositories of its counted descendants in
    landing order (`land_order`, then `repo_order`, then by name)."""
    data = snap.graph_data()
    node = data.nodes.get(node_id)
    if node is None:
        return []
    if node.kind not in CONTAINERS:
        return [node.target_repo] if node.target_repo else []
    found = {
        child.target_repo
        for d in snap.counted_descendants(node_id)
        if (child := data.nodes.get(d)) is not None and child.target_repo
    }
    order = [*node.land_order, *repo_order]
    return sorted(found, key=lambda r: (order.index(r) if r in order else len(order), r))


def _locked_files(node_id: str, action: Action | None, snap: Snapshot) -> list[str]:
    """The files a claim of `node_id` locks: its declared files, or for a container that
    declares none, the union of its counted descendants'."""
    if action not in (Action.IMPLEMENT, Action.FIX):
        return []
    data = snap.graph_data()
    node = snap.nodes[node_id]
    own = declared_files_of(data.nodes.get(node_id), data.verifications.get(node_id, []))
    if own or node.kind not in CONTAINERS:
        return [locked_key(node.repo, f) for f in own]
    keys = [
        locked_key(snap.nodes[d].repo, f)
        for d in snap.counted_descendants(node_id)
        for f in declared_files_of(data.nodes.get(d), data.verifications.get(d, []))
    ]
    return list(dict.fromkeys(keys))


def _conflicts(files: list[str], snap: Snapshot) -> dict[str, str]:
    data = snap.graph_data()
    wanted = set(files)
    found: dict[str, str] = {}
    for lock in data.file_locks:
        if lock.file_path not in wanted:
            continue
        lease = data.leases.get(lock.task_id)
        if lease is not None and lease_alive(
            lease.ttl_seconds, lease.last_heartbeat, data.built_at
        ):
            found[lock.file_path] = f"Task: {lock.task_id}, Agent: {lease.agent_id}"
    return found


def blocked_reason(
    node: Node, snap: Snapshot, action: Action | None, *, repo_order: Sequence[str] = ()
) -> str | None:
    """Why `node` cannot be claimed now, the first reason in claimability order, everything but a
    condition; None when nothing here blocks it."""
    data = snap.graph_data()
    live = [j for j in data.jobs.get(node.id, []) if j.state in _LIVE_JOBS]
    if live:
        job = live[0]
        if job.kind == JobKind.SYNC:
            return f"syncing {job.target}"
        return f"landing job {job.id} is {job.state}"
    lease = data.leases.get(node.id)
    if lease is not None and lease_alive(lease.ttl_seconds, lease.last_heartbeat, data.built_at):
        return f"held by {lease.agent_id}"
    edges = snap.inherited_edges(node.id)
    decisions = [d for d in edges if snap.status(d) == DecisionStatus.OPEN]
    if decisions:
        return f"awaiting decision {', '.join(decisions)}"
    waiting = [
        d for d in edges if isinstance(snap.status(d), Status) and not satisfied(snap, node.id, d)
    ]
    if waiting:
        return f"waits on {', '.join(waiting)}"
    if action is None:
        return f"{node.status} has no next action"
    if action == Action.IMPLEMENT and not node.target_repo:
        return "no target_repo: a task is cut and landed in its target repository"
    if action == Action.MERGE and not repos_of(snap, node.id, repo_order):
        return "nothing to land: no task under it names a target_repo"
    conflicts = _conflicts(_locked_files(node.id, action, snap), snap)
    if conflicts:
        return f"declared files locked: {', '.join(sorted(conflicts))}"
    return None


def _in_scope(snap: Snapshot, node: Node, specs: list[str] | None) -> bool:
    if specs is None:
        return True
    spec = node.id if node.kind == NodeKind.SPEC else _ancestor_spec(snap, node.id)
    return (spec or "none") in specs


def _ancestor_spec(snap: Snapshot, node_id: str) -> str | None:
    visited: set[str] = set()
    current = node_id
    while True:
        parent = snap.parent(current)
        if parent is None or parent in visited:
            return None
        visited.add(parent)
        if snap.nodes[parent].kind == NodeKind.SPEC:
            return parent
        current = parent


def candidates(
    snap: Snapshot,
    specs: list[str] | None,
    *,
    next_step: Callable[[Node], tuple[Action | None, str | None]] = next_step,
    blocked_reason: Callable[[Node, Snapshot, Action | None], str | None] = blocked_reason,
) -> tuple[list[Candidate], list[str]]:
    """Every claimable node with the step it would take next, later steps first within a
    priority, and the reason each held node cannot be claimed now.

    `next_step` and `blocked_reason` default to this module's own pure rules, over `snap` alone;
    `Claims` calls this with its own bound methods instead, so a condition still runs its command
    exactly as a real claim would, and a monkeypatch of `Claims.next_step` still reaches here.
    """
    found: list[Candidate] = []
    held: list[str] = []
    data = snap.graph_data()
    for node in data.nodes.values():
        if node.kind == NodeKind.DECISION or not _in_scope(snap, node, specs):
            continue
        waiting = next(
            (j for j in data.jobs.get(node.id, []) if j.state == JobState.NEEDS_AGENT), None
        )
        if waiting is not None:
            lease = data.leases.get(node.id)
            if lease is not None and lease.ttl_seconds is None:
                job_action = Action.MERGE if waiting.kind == JobKind.LAND else Action.SYNC
                found.append(Candidate(node, job_action, "sonnet", waiting.id, [waiting.repo]))
            continue
        if Status(node.status) in IN_STEP:
            continue
        try:
            action, model = next_step(node)
        except LifecycleError as exc:
            # One node the lifecycle cannot read must not stop the wave for every other node.
            held.append(f"{node.id}: {exc}")
            continue
        if action is None or model is None:
            continue
        reason = blocked_reason(node, snap, action)
        if reason is not None:
            held.append(f"{node.id}: {reason}")
            continue
        found.append(Candidate(node, action, model, None, repos_of(snap, node.id)))
    return sorted(found, key=lambda c: (_STAGE[c.action], -c.node.priority, c.node.id)), held


def select(
    candidates: list[Candidate],
    snap: Snapshot,
    size: int,
    max_strong: int,
    exclude: Sequence[str] = (),
    hold_merge: Sequence[str] = (),
) -> Selection:
    """The wave `candidates` fills `size` slots with, `max_strong` of them opus/fable, skipping
    `exclude` and holding `hold_merge`'s merges back."""
    data = snap.graph_data()
    excluded = set(exclude)
    merge_held = set(hold_merge)
    held: list[str] = []
    chosen: list[dict[str, object]] = []
    taken: set[str] = set()
    chain_holders: dict[str, dict[str, str]] = {}
    strong_free = max_strong
    waiting = 0
    for cand in candidates:
        node = cand.node
        if node.id in excluded:
            held.append(f"{node.id}: excluded by args")
            continue
        if cand.action == Action.MERGE and node.id in merge_held:
            held.append(f"{node.id}: merge held by the dispatcher")
            continue
        repo = node.target_repo or ""
        migration = snap.nodes[node.id].writes_migration
        files = (
            [
                locked_key(node.target_repo, f)
                for f in declared_files_of(node, data.verifications.get(node.id, []))
            ]
            if cand.action in (Action.IMPLEMENT, Action.FIX)
            else []
        )
        why: list[str] = []
        if cand.action == Action.IMPLEMENT and migration:
            if repo not in chain_holders:
                chain_holders[repo] = migration_holders(snap, repo)
            holder = chain_holders[repo].get(node.id)
            if holder is not None:
                why.append(f"{repo} migration chain held by {holder}")
        if taken.intersection(files):
            why.append("declared_files overlap a node chosen this wave")
        if cand.model in STRONG and strong_free <= 0:
            why.append("no free opus/fable slot")
        if not why and len(chosen) >= size:
            waiting += 1
            continue
        if why:
            held.append(f"{node.id}: {'; '.join(why)}")
            continue
        chosen.append(
            {
                "id": node.id,
                "kind": node.kind,
                "action": cand.action,
                "model": cand.model,
                "repos": cand.repos,
                "requires": node.requires,
                "migration": migration,
                "job": cand.job,
            }
        )
        taken.update(files)
        if cand.model in STRONG:
            strong_free -= 1
    return Selection(chosen=chosen, held=held, waiting_for_slot=waiting)
