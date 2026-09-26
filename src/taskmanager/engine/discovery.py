"""One dispatch wave's batch: every claimable node with the step it would take next.

Discovery reads and never claims; each chosen node is claimed by `tm task start`, which runs the
same checks again inside its transaction.
"""

import json
from dataclasses import dataclass

from taskmanager.core.enums import NodeKind
from taskmanager.core.lifecycle import LifecycleError
from taskmanager.core.models import Node
from taskmanager.core.status import IN_STEP, Action, JobKind, JobState, Status
from taskmanager.engine.claims import Claims
from taskmanager.engine.routing import STRONG
from taskmanager.engine.stepgraph import Snapshot, migration_holders

# Later steps first, so a wave drains work already under way before it starts more.
_STAGE = {Action.MERGE: 0, Action.SYNC: 0, Action.FIX: 1, Action.REVIEW: 2, Action.IMPLEMENT: 3}


@dataclass(frozen=True)
class _Candidate:
    node: Node
    action: Action
    model: str
    job: str | None
    repos: list[str]


def djb2(payload: str) -> int:
    """The `__CHECK h=` checksum `tm wave discover` prints after its payload: a caller that
    echoes the payload back through a model can reject a transcription that is not byte-exact."""
    checksum = 5381
    for byte in payload.encode("utf-8"):
        checksum = (checksum * 33 + byte) & 0xFFFFFFFF
    return checksum


def _in_scope(claims: Claims, node: Node, specs: list[str] | None) -> bool:
    if specs is None:
        return True
    spec = (
        node.id
        if node.kind == NodeKind.SPEC
        else claims.nodes.get_ancestor_of_kind(node.id, NodeKind.SPEC)
    )
    return (spec or "none") in specs


def _candidates(
    claims: Claims, snap: Snapshot, specs: list[str] | None, held: list[str]
) -> list[_Candidate]:
    found: list[_Candidate] = []
    assert snap.data is not None, "discover() builds its snapshot through SnapshotBuilder.build()"
    data = snap.data
    for node in claims.nodes.list_nodes():
        if node.kind == NodeKind.DECISION or not _in_scope(claims, node, specs):
            continue
        waiting = next(
            (j for j in data.jobs.get(node.id, []) if j.state == JobState.NEEDS_AGENT), None
        )
        if waiting is not None:
            lease = data.leases.get(node.id)
            if lease is not None and lease.ttl_seconds is None:
                job_action = Action.MERGE if waiting.kind == JobKind.LAND else Action.SYNC
                found.append(_Candidate(node, job_action, "sonnet", waiting.id, [waiting.repo]))
            continue
        if Status(node.status) in IN_STEP:
            continue
        try:
            action, model = claims.next_step(node)
        except LifecycleError as exc:
            # One node the lifecycle cannot read must not stop the wave for every other node.
            held.append(f"{node.id}: {exc}")
            continue
        if action is None or model is None:
            continue
        reason = claims.blocked_reason(node, snap, action)
        if reason is not None:
            held.append(f"{node.id}: {reason}")
            continue
        found.append(_Candidate(node, action, model, None, claims.repos_of(node.id)))
    return sorted(found, key=lambda c: (_STAGE[c.action], -c.node.priority, c.node.id))


def discover(
    claims: Claims,
    specs: list[str] | None,
    session: str,
    slots: int,
    max_strong: int,
    exclude: list[str] | None = None,
    hold_merge: list[str] | None = None,
) -> tuple[str, int]:
    claims.sweep()
    snap = claims.snapshots.build()
    excluded = set(exclude or [])
    merge_held = set(hold_merge or [])
    # A lease parked for an agent (no TTL) is a stopped job nobody runs, so it fills no slot.
    mine = [
        lease
        for lease in claims.runtime.list_leases()
        if lease.session_id == session and lease.ttl_seconds is not None
    ]
    free = slots - len(mine)
    strong_free = max_strong - sum(lease.model in STRONG for lease in mine)
    held: list[str] = []
    chosen: list[dict[str, object]] = []
    taken: set[str] = set()
    chain_holders: dict[str, dict[str, str]] = {}
    waiting = 0
    for cand in _candidates(claims, snap, specs, held):
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
            claims.nodes.declared_files(node.id)
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
        if not why and len(chosen) >= free:
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
    payload = json.dumps(
        {"chosen": chosen, "held": held, "waiting_for_slot": waiting, "mine": len(mine)},
        separators=(",", ":"),
        sort_keys=True,
    )
    return payload, len(chosen)
