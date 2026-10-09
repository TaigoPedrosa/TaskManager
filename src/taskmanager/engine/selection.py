"""Which node the graph would claim next, and which of those fit one wave: pure functions of a
`Snapshot`, so `Claims`, `tm wave discover` and the wave simulator choose exactly alike.

A live condition needs a command run, which a snapshot alone cannot do, so `blocked_reason` here
never runs one; `Claims.blocked_reason` layers that live check on top, over the same snapshot. A
caller with no live check available (the wave simulator, run ahead of any real claim) may pass
`cached_conditions`, the same condition-result cache `DisplayView` reads once per view, instead.
"""

from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field

from taskmanager.core.enums import CONTAINERS, NodeKind
from taskmanager.core.lifecycle import LifecycleError, claim, fix_round, next_action
from taskmanager.core.models import Node
from taskmanager.core.status import (
    IN_STEP,
    Action,
    ConditionStage,
    DecisionStatus,
    JobKind,
    JobState,
    Status,
)
from taskmanager.db.cache_repo import _command_hash
from taskmanager.db.node_repo import declared_files_of, is_locked_path, locked_key
from taskmanager.db.runtime_repo import lease_alive
from taskmanager.engine.chains import satisfied
from taskmanager.engine.config import ProjectConfig
from taskmanager.engine.routing import STRONG, model_for
from taskmanager.engine.snapshot import SnapshotBuilder, apply_cycle, cycle_in
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


def next_step(node: Node, snap: Snapshot) -> tuple[Action | None, str | None]:
    """The action a claim would take now, and the model it would name, from the node's own
    stored cycle and its declared files in `snap`."""
    cycle = cycle_in(snap, node)
    action = next_action(cycle)
    if action is None:
        return None, None
    claimed = claim(cycle)
    return action, model_for(action, apply_cycle(node, claimed), fix_round(claimed))


def ordered_repos(
    found: set[str], land_order: Sequence[str], repo_order: Sequence[str]
) -> list[str]:
    """`found`, `land_order` first, then `repo_order`, then by name -- the one ordering
    `Operations.repos_of` (a single live node) and `repos_of` below (a whole snapshot) both sort
    by, so a container's repository order never depends on which of the two read it."""
    order = [*land_order, *repo_order]
    return sorted(found, key=lambda r: (order.index(r) if r in order else len(order), r))


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
    return ordered_repos(found, node.land_order, repo_order)


def _locked_files(node_id: str, action: Action | None, snap: Snapshot) -> list[str]:
    """The files a claim of `node_id` locks, through the one lock-set implementation
    (`SnapshotBuilder.lock_set`) `Claims` itself locks through -- so the set a claim takes and the
    set discovery checks can never drift apart."""
    if action not in (Action.IMPLEMENT, Action.FIX):
        return []
    return SnapshotBuilder.lock_set(node_id, snap)


def _conflicts(files: list[str], snap: Snapshot) -> dict[str, str]:
    return SnapshotBuilder.conflicts(files, snap)


def blocked_reason_before_condition(node: Node, snap: Snapshot) -> str | None:
    """The claimability checks a condition's command must never wait behind: a live job, a held
    lease, an open decision or an unsatisfied edge. None when none of these blocks `node`."""
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
    return None


def blocked_reason_after_condition(
    node: Node, snap: Snapshot, action: Action | None, *, repo_order: Sequence[str] = ()
) -> str | None:
    """The claimability checks that come after a condition's command has run: whether there is a
    next action at all, then target_repo and file-lock conflicts. None when none of these blocks
    `node`."""
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


def _condition_reason_cached(
    node: Node,
    snap: Snapshot,
    action: Action | None,
    cached_conditions: Mapping[tuple[str, int], tuple[str, int]],
) -> str | None:
    """The same condition check `Claims._condition_reason` runs live, read instead from a
    condition-result cache taken once for the whole call (`DisplayView`'s own `_unmet`): a
    simulated wave has no real claim to run a command under, so a stale cached result -- or none
    at all, which reads as met -- is the only signal it can ever have."""
    data = snap.graph_data()
    stages = {ConditionStage.CLAIM}
    if action == Action.MERGE:
        stages.add(ConditionStage.LANDING)
    for condition in data.conditions.get(node.id, []):
        if condition.stage not in stages:
            continue
        entry = cached_conditions.get((node.id, condition.idx))
        if entry is None:
            continue
        command_hash, exit_code = entry
        if command_hash == _command_hash(condition.command) and exit_code != 0:
            return f"condition unmet: {condition.needs}"
    return None


def blocked_reason(
    node: Node,
    snap: Snapshot,
    action: Action | None,
    *,
    repo_order: Sequence[str] = (),
    cached_conditions: Mapping[tuple[str, int], tuple[str, int]] | None = None,
) -> str | None:
    """Why `node` cannot be claimed now, the first reason in claimability order, everything but a
    live condition check; None when nothing here blocks it. `cached_conditions`, when given, folds
    in the same cached condition result a display reads, ahead of "no next action" exactly where a
    live claim's own condition check runs."""
    reason = blocked_reason_before_condition(node, snap)
    if reason is not None:
        return reason
    if cached_conditions is not None:
        reason = _condition_reason_cached(node, snap, action, cached_conditions)
        if reason is not None:
            return reason
    return blocked_reason_after_condition(node, snap, action, repo_order=repo_order)


def gated_repos(config: ProjectConfig) -> frozenset[str]:
    return frozenset(repo for repo, found in config.repos.items() if "main" in found.gates)


def ungated_reason(repos: Sequence[str], gated: Collection[str]) -> str | None:
    """Why a node landing in `repos` is held when only `gated` have a main gate: its chain
    could only stop at its landing."""
    repo = next((r for r in repos if r not in gated), None)
    if repo is None:
        return None
    return (
        f"repos.{repo}.gates.main is not configured, so nothing lands in {repo}; set it with "
        f'`tm config set repos.{repo}.gates.main.command "<your test command>"`'
    )


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
    repo_order: Sequence[str] = (),
    next_step: Callable[[Node, Snapshot], tuple[Action | None, str | None]] = next_step,
    blocked_reason: Callable[[Node, Snapshot, Action | None], str | None] = blocked_reason,
    gated: Collection[str] | None = None,
) -> tuple[list[Candidate], list[str]]:
    """Every claimable node with the step it would take next, later steps first within a
    priority, and the reason each held node cannot be claimed now.

    `next_step` and `blocked_reason` default to this module's own pure rules, over `snap` alone;
    `Claims` calls this with its own bound methods instead, so a condition still runs its command
    exactly as a real claim would, and a monkeypatch of `Claims.next_step` still reaches here.
    `repo_order` only orders a `Candidate`'s own `repos`; a `blocked_reason` bound to `Claims`
    carries its own copy for the "nothing to land" check, so a caller passing a custom
    `blocked_reason` must give it the same `repo_order` itself.

    `gated`, when given, names the repositories with a main gate: a node touching any other is
    held now, since its chain could only stop at its landing.
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
            action, model = next_step(node, snap)
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
        repos = repos_of(snap, node.id, repo_order)
        ungated = None if gated is None else ungated_reason(repos, gated)
        if ungated is not None:
            held.append(f"{node.id}: {ungated}")
            continue
        found.append(Candidate(node, action, model, None, repos))
    return sorted(found, key=lambda c: (_STAGE[c.action], -c.node.priority, c.node.id)), held


def _wave_files(node: Node, action: Action | None, snap: Snapshot) -> list[str]:
    if action not in (Action.IMPLEMENT, Action.FIX):
        return []
    return [
        locked_key(node.target_repo, f)
        for f in declared_files_of(node, snap.graph_data().verifications.get(node.id, []))
        if is_locked_path(f)
    ]


def _reserved(exclude: Sequence[str], snap: Snapshot) -> set[str]:
    """The files the excluded nodes' next claims lock. An excluded node is one a concurrent run of
    the same tick is about to claim, and that run may be scoped to another spec, so each is read
    from `snap` whether or not it is a candidate here."""
    nodes = snap.graph_data().nodes
    taken: set[str] = set()
    for node_id in exclude:
        node = nodes.get(node_id)
        if node is None:
            continue
        try:
            action, _ = next_step(node, snap)
        except LifecycleError:
            # A node the lifecycle cannot read cannot be claimed either, so it locks nothing.
            continue
        taken.update(_wave_files(node, action, snap))
    return taken


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
    excluded = set(exclude)
    merge_held = set(hold_merge)
    held: list[str] = []
    chosen: list[dict[str, object]] = []
    taken = _reserved(exclude, snap)
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
        files = _wave_files(node, cand.action, snap)
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
