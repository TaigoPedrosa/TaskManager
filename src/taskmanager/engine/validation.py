"""The rules every write must keep. A write is described by the snapshot before it, the snapshot
after it, and the nodes it touched; a refusal names the node, the rule broken and the fix.

A step's own close (complete, review, landing, release) is the lease holder's write, made
through the lifecycle, and does not come here: rule 7 guards a busy node against everyone else."""

from dataclasses import dataclass
from typing import Final, Protocol

from taskmanager.core.enums import NodeKind, VerificationType
from taskmanager.core.lifecycle import REOPENABLE
from taskmanager.core.status import EXITS, IN_STEP, ON_TARGET, Merge, Status
from taskmanager.engine.chains import TOP, landing_target, target
from taskmanager.engine.git import valid_branch
from taskmanager.engine.snapshot import ORIGIN_MAIN
from taskmanager.engine.stepgraph import (
    SnapNode,
    Snapshot,
    find_cycle,
    format_cycle,
    migration_writers,
)
from taskmanager.engine.verification import codegraph_regex

_SET_ASIDE_OR_FAILED = EXITS | {Status.FAILED}
# Work whose code never lands again, so nothing reads where it would.
_LANDS_NO_MORE: Final = frozenset({Status.COMPLETED, Status.SUPERSEDED})
# What a `sensitive:` key may name: a fix touching one of these gets one review scoped to its
# findings before it lands.
SENSITIVE_AREAS: Final = ("tenant", "rls", "crypto", "migration")


@dataclass(frozen=True)
class Refusal:
    node_id: str
    rule: int
    message: str


class BranchFacts(Protocol):
    def branch_exists(self, node_id: str) -> bool: ...
    def base_matches(self, node_id: str, new_target: str, new_top: str) -> bool: ...


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
    if n.status == Status.LANDED and not n.review:
        refusals.append(
            Refusal(
                n.id,
                11,
                f"{n.id}: review is off at LANDED, so nothing reviews or completes it; reset it "
                "to COMPLETED first, or turn review on",
            )
        )
    if n.claimed_from == Status.LANDED and not n.review:
        refusals.append(
            Refusal(
                n.id,
                11,
                f"{n.id}: review is off while its review after landing is in step, and a release "
                "or a sweep returns it to LANDED, where nothing reviews or completes it; let that "
                "review end, or release it and reset it to COMPLETED first",
            )
        )
    if n.merge == Merge.PARENT and (n.kind == NodeKind.SPEC or n.parent is None):
        refusals.append(
            Refusal(
                n.id,
                3,
                f"{n.id}: a spec or a parentless node has no parent branch to land on; "
                f"set merge=spec to land on its target {n.top}",
            )
        )
    unknown = [area for area in n.sensitive if area not in SENSITIVE_AREAS]
    if unknown:
        refusals.append(
            Refusal(
                n.id,
                9,
                f"{n.id}: sensitive names {', '.join(map(repr, unknown))}; it takes "
                f"{', '.join(SENSITIVE_AREAS)}",
            )
        )
    refusals += _origin_main(n)
    if n.land_on is not None and n.kind != NodeKind.SPEC:
        refusals.append(
            Refusal(
                n.id,
                12,
                f"{n.id}: land_on is set on a spec only, and {n.id} is a {n.kind}; unset it, "
                "or set it on its spec",
            )
        )
    elif n.land_on is not None and not valid_branch(n.land_on):
        refusals.append(
            Refusal(n.id, 12, f"{n.id}: land_on '{n.land_on}' is not a branch name git accepts")
        )
    return refusals


def _origin_main(n: SnapNode) -> list[Refusal]:
    on_parent = n.merge == Merge.PARENT
    if not (n.literal_origin_main and (on_parent or f"origin/{n.top}" != ORIGIN_MAIN)):
        return []
    where = "its parent's branch" if on_parent else f"its target {n.top}"
    return [
        Refusal(
            n.id,
            5,
            f"{n.id}: lands on {where} but a test_command names {ORIGIN_MAIN}; "
            "read the landing target from TM_VERIFY_REF instead",
        )
    ]


def _unreviewed_on_target(before: Snapshot, after: Snapshot, n: SnapNode) -> list[Refusal]:
    parent = after.nodes.get(n.parent) if n.parent is not None else None
    if parent is None or not parent.review or n.review or n.merge != Merge.SPEC:
        return []
    # Only the write that makes this shape is refused: a node already in it, left as it is,
    # never blocks a write around it.
    old = before.nodes.get(n.id)
    old_parent = before.nodes.get(parent.id)
    if (
        old is not None
        and old_parent is not None
        and old_parent.review
        and (old.parent, old.review, old.merge) == (n.parent, n.review, n.merge)
    ):
        return []
    return [
        Refusal(
            n.id,
            10,
            f"{n.id}: lands on its target {n.top} with review off, so its code would land there "
            f"unreviewed: {parent.id}'s review reads only what lands on its branch; set "
            "merge=parent, or turn review on",
        )
    ]


def _retarget(
    before: Snapshot, after: Snapshot, n: SnapNode, branches: BranchFacts
) -> list[Refusal]:
    old = before.nodes.get(n.id)
    if old is None:
        return []
    new_target = landing_target(after, n.id)
    if new_target == landing_target(before, n.id):
        return []
    if not branches.branch_exists(n.id) or branches.base_matches(n.id, new_target, n.top):
        return []
    # A branch cut from a container's branch would carry that container's unreviewed code.
    where = new_target.removeprefix(TOP)
    refused = f"{n.id}: its branch exists and was not cut from {where}"
    # Only a node already set aside or failed reopens, and landed or replaced work never does.
    if n.status in ON_TARGET:
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


def retargets(before: Snapshot, after: Snapshot, branches: BranchFacts) -> list[Refusal]:
    """Rule 4 alone, over every node whose code still lands: for a write that moves where whole
    chains land rather than any one node."""
    live = [n for _, n in sorted(after.nodes.items()) if n.status not in _LANDS_NO_MORE]
    return [refusal for n in live for refusal in _retarget(before, after, n, branches)]


def moved_tops(before: Snapshot, after: Snapshot) -> list[Refusal]:
    """Rules 5 and 13 over the work that still lands, for a write that moves where whole chains
    land: a node moved off the target its `origin/main` check reads, and a wait newly drawn
    across targets."""
    moved = [
        n
        for _, n in sorted(after.nodes.items())
        if n.status not in _LANDS_NO_MORE
        and (old := before.nodes.get(n.id)) is not None
        and old.top != n.top
    ]
    refusals = [refusal for n in moved for refusal in _origin_main(n)]
    crossed = _crossings(after, _LANDS_NO_MORE)
    already = _crossings(before, _LANDS_NO_MORE).keys()
    return refusals + [crossed[k] for k in sorted(crossed.keys() - already)]


def _placement(before: Snapshot, after: Snapshot, n: SnapNode) -> list[Refusal]:
    old = before.nodes.get(n.id)
    arrived = old is None or old.parent != n.parent
    # Work that left a container's count, or that already landed, coming back into play.
    reopened = old is not None and (
        (old.status in _SET_ASIDE_OR_FAILED and n.status not in _SET_ASIDE_OR_FAILED)
        or (old.status in ON_TARGET and n.status not in ON_TARGET)
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
        if holder.status in ON_TARGET:
            # Its own code is on its target already, so nothing would carry a new child's on.
            refusals.append(
                Refusal(n.id, 6, f"{n.id}: {ancestor} is {holder.status}; file a new plan instead")
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


def _codegraph_patterns(before: Snapshot, after: Snapshot, node_id: str) -> list[Refusal]:
    if before.data is None or after.data is None:
        return []
    # Only the write that stores the check is refused, so one already stored never blocks a
    # write around it.
    stored = {v.id for v in before.data.verifications.get(node_id, ())}
    refusals: list[Refusal] = []
    for v in after.data.verifications.get(node_id, ()):
        if v.verification_type != VerificationType.CODEGRAPH_QUERY or v.id in stored:
            continue
        try:
            codegraph_regex(v.expected_pattern)
        except ValueError as e:
            refusals.append(
                Refusal(node_id, 14, f"{node_id}: codegraph_query {v.target_path!r} {e}")
            )
    return refusals


def _crossings(
    s: Snapshot, settled: frozenset[Status] = frozenset()
) -> dict[tuple[str, ...], Refusal]:
    """Every wait the step graph draws between two targets, which no meeting node can satisfy:
    a dependency edge, once per node it gates, and a migration chain's link. A node in `settled`
    neither waits nor is waited on."""
    targets: dict[str, str] = {}

    def on(node_id: str) -> str:
        if node_id not in targets:
            targets[node_id] = target(s, node_id)
        return targets[node_id]

    def work(node_id: str) -> bool:
        return node_id in s.nodes and s.nodes[node_id].kind != NodeKind.DECISION

    found: dict[tuple[str, ...], Refusal] = {}
    for owner, dep in s.edges:
        if not (work(owner) and work(dep)) or s.status(dep) in {Status.SUPERSEDED, *settled}:
            continue
        for d in [owner, *s.descendants(owner)]:
            key = (owner, dep, on(d), on(dep))
            if not work(d) or s.status(d) in settled or on(d) == on(dep) or key in found:
                continue
            who = owner if d == owner else f"{d} under it"
            found[key] = Refusal(
                owner,
                13,
                f"{owner}: depends on {dep}, which lands on {on(dep)}, but {who} lands on "
                f"{on(d)}; remove the edge, or land both on one target",
            )
    for repo in sorted({n.repo for n in s.nodes.values() if n.writes_migration and n.repo}):
        writers = sorted(n.id for n in migration_writers(s, repo))
        for i, a in enumerate(writers):
            for b in writers[i + 1 :]:
                if on(a) != on(b):
                    found[(repo, a, b, on(a), on(b))] = Refusal(
                        a,
                        13,
                        f"{a}: writes {repo}'s migrations on {on(a)} while {b} writes them on "
                        f"{on(b)}; land one of them first, or land both on one target",
                    )
    return found


def validate(
    before: Snapshot, after: Snapshot, touched: set[str], branches: BranchFacts
) -> list[Refusal]:
    present = {node_id for node_id in touched if node_id in after.nodes}
    # A write to a container can break a rule its children keep only through it, and a spec's
    # `land_on` moves every node below it that lands at the top.
    retopped = {
        node_id
        for node_id, n in after.nodes.items()
        if (old := before.nodes.get(node_id)) is not None and old.top != n.top
    }
    checked = (
        present | {child for node_id in present for child in after.children(node_id)} | retopped
    )
    refusals: list[Refusal] = []
    for node_id in sorted(checked):
        n = after.nodes[node_id]
        if n.kind == NodeKind.DECISION:
            continue
        refusals += _flags(after, n)
        refusals += _unreviewed_on_target(before, after, n)
        refusals += _retarget(before, after, n, branches)
        refusals += _placement(before, after, n)
        refusals += _busy(before, after, n)
        refusals += _codegraph_patterns(before, after, node_id)
    # Only the write that draws a wait across targets is refused, never one around it.
    crossed = _crossings(after)
    refusals += [crossed[k] for k in sorted(crossed.keys() - _crossings(before).keys())]
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
