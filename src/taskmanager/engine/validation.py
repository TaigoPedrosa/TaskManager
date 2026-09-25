"""The rules every write must keep. A write is described by the snapshot before it, the snapshot
after it, and the nodes it touched; a refusal names the node, the rule broken and the fix.

A step's own close (complete, review, landing, release) is the lease holder's write, made
through the lifecycle, and does not come here: rule 7 guards a busy node against everyone else."""

from dataclasses import dataclass
from typing import Protocol

from taskmanager.core.enums import NodeKind
from taskmanager.core.lifecycle import REOPENABLE
from taskmanager.core.status import EXITS, IN_STEP, Merge, Status
from taskmanager.engine.chains import MAIN, landing_target
from taskmanager.engine.stepgraph import SnapNode, Snapshot, find_cycle, format_cycle

_SET_ASIDE_OR_FAILED = EXITS | {Status.FAILED}


@dataclass(frozen=True)
class Refusal:
    node_id: str
    rule: int
    message: str


class BranchFacts(Protocol):
    def branch_exists(self, node_id: str) -> bool: ...
    def base_matches(self, node_id: str, new_target: str) -> bool: ...


def _flags(after: Snapshot, n: SnapNode) -> list[Refusal]:
    refusals: list[Refusal] = []
    if n.fix and not n.review:
        refusals.append(Refusal(n.id, 1, f"{n.id}: fix is on with review off; turn review on"))
    if n.review and not n.fix:
        parent = after.nodes.get(n.parent) if n.parent is not None else None
        fixed_above = (
            n.merge == Merge.PARENT and parent is not None and parent.review and parent.fix
        )
        if not fixed_above:
            refusals.append(
                Refusal(
                    n.id,
                    2,
                    f"{n.id}: review is on with fix off, so a rejection must land on a parent "
                    "that reviews and fixes it; set merge=parent under such a parent, or "
                    "turn fix on",
                )
            )
    if n.merge == Merge.PARENT and (n.kind == NodeKind.SPEC or n.parent is None):
        refusals.append(
            Refusal(n.id, 3, f"{n.id}: a spec or a parentless node lands on main; set merge=main")
        )
    if n.merge == Merge.PARENT and n.literal_origin_main:
        refusals.append(
            Refusal(
                n.id,
                5,
                f"{n.id}: lands on its parent's branch but a test_command names origin/main; "
                "read the landing target from TM_VERIFY_REF instead",
            )
        )
    return refusals


def _retarget(
    before: Snapshot, after: Snapshot, n: SnapNode, branches: BranchFacts
) -> list[Refusal]:
    if n.id not in before.nodes or not branches.branch_exists(n.id):
        return []
    new_target = landing_target(after, n.id)
    if new_target == landing_target(before, n.id) or branches.base_matches(n.id, new_target):
        return []
    # A branch cut from a container's branch would carry that container's unreviewed code.
    where = "main" if new_target == MAIN else new_target
    refused = f"{n.id}: its branch exists and was not cut from {where}"
    # Only a node already set aside or failed reopens, and landed or replaced work never does.
    if n.status == Status.COMPLETED:
        return [
            Refusal(n.id, 4, f"{refused}; its code has landed; file a new task to land on {where}")
        ]
    if n.status == Status.SUPERSEDED:
        return [
            Refusal(
                n.id, 4, f"{refused}; its replacement carries the work; change where that lands"
            )
        ]
    if n.status in REOPENABLE:
        steps = ""
    elif n.status in IN_STEP:
        steps = "wait for its step to end, or stop it, then defer it, then "
    else:
        steps = "defer it, then "
    return [
        Refusal(
            n.id, 4, f"{refused}; {steps}reopen it with --new-branch before changing where it lands"
        )
    ]


def _placement(before: Snapshot, after: Snapshot, n: SnapNode) -> list[Refusal]:
    old = before.nodes.get(n.id)
    arrived = old is None or old.parent != n.parent
    # Work that left a container's count, or that already landed, coming back into play.
    reopened = old is not None and (
        (old.status in _SET_ASIDE_OR_FAILED and n.status not in _SET_ASIDE_OR_FAILED)
        or (old.status == Status.COMPLETED and n.status != Status.COMPLETED)
    )
    if not (arrived or reopened):
        return []
    refusals: list[Refusal] = []
    ancestor = n.parent
    while ancestor is not None and ancestor in after.nodes:
        # Judged by the ancestor's state before this write: an ancestor created in the same
        # write (a restore bringing a completed plan with its children) refuses nothing.
        holder = before.nodes.get(ancestor)
        if holder is None:
            ancestor = after.nodes[ancestor].parent
            continue
        if holder.status == Status.COMPLETED:
            refusals.append(
                Refusal(n.id, 6, f"{n.id}: {ancestor} is COMPLETED; file a new plan instead")
            )
        elif holder.status in _SET_ASIDE_OR_FAILED:
            # The rollup keeps an exit someone chose, so the ancestor would never count this
            # child's work.
            refusals.append(
                Refusal(n.id, 6, f"{n.id}: {ancestor} is {holder.status}; reopen {ancestor} first")
            )
        elif holder.busy or holder.status in IN_STEP:
            # A step whose lease lapsed still owns the ancestor until a sweep returns it, and
            # the sweep does not re-derive it from a child added meanwhile.
            refusals.append(
                Refusal(
                    n.id,
                    6,
                    f"{n.id}: {ancestor} is in a step; wait for it to end, or stop it",
                )
            )
        ancestor = holder.parent
    return refusals


def _deps(s: Snapshot, node_id: str) -> set[str]:
    return {dep for d, dep in s.edges if d == node_id}


def _busy(before: Snapshot, after: Snapshot, n: SnapNode) -> list[Refusal]:
    old = before.nodes.get(n.id)
    if old is None or not old.busy:
        return []
    old_deps, new_deps = _deps(before, n.id), _deps(after, n.id)
    linked_decision = any(
        after.nodes[dep].kind == NodeKind.DECISION
        for dep in new_deps - old_deps
        if dep in after.nodes
    )
    dropped_edge = not old_deps <= new_deps
    moved_or_set = old.parent != n.parent or old.status != n.status
    if not (moved_or_set or linked_decision or dropped_edge):
        return []
    return [
        Refusal(
            n.id,
            7,
            f"{n.id} holds a live lease or a running job; wait for its step to end, or stop "
            "it, then retry",
        )
    ]


def validate(
    before: Snapshot, after: Snapshot, touched: set[str], branches: BranchFacts
) -> list[Refusal]:
    present = {node_id for node_id in touched if node_id in after.nodes}
    # A write to a container can break a rule its children keep only through it.
    checked = present | {child for node_id in present for child in after.children(node_id)}
    refusals: list[Refusal] = []
    for node_id in sorted(checked):
        n = after.nodes[node_id]
        if n.kind == NodeKind.DECISION:
            continue
        refusals += _flags(after, n)
        refusals += _retarget(before, after, n, branches)
        refusals += _placement(before, after, n)
        refusals += _busy(before, after, n)
    cycle = find_cycle(after)
    if cycle is not None:
        refusals.append(
            Refusal(
                cycle[0].rsplit(".", 1)[0],
                8,
                f"this write makes the step graph cyclic: {format_cycle(cycle)}; "
                "remove an edge on the cycle or change where a node on it lands",
            )
        )
    return refusals
