"""Where a node lands and what it builds on, decided from stored statuses, recorded landings and
the tree alone: no git call and no repository comparison, so satisfaction is the same in every
repository."""

from typing import Final, Protocol

from taskmanager.core.status import DecisionStatus, Merge, Status

# A chain ends at `TOP:<branch>`, its target. No node id collides with it: git refuses `tm/<id>`
# with a ':' in it.
TOP: Final = "TOP:"


class Tree(Protocol):
    def parent(self, node_id: str) -> str | None: ...
    def merge(self, node_id: str) -> Merge: ...
    def status(self, node_id: str) -> Status | DecisionStatus: ...
    def top(self, node_id: str) -> str: ...
    def on_target(self, node_id: str) -> bool: ...


def landing_target(t: Tree, node_id: str) -> str:
    parent = t.parent(node_id)
    # A parentless node with merge=parent is refused at write time; reading it as landing on
    # its target keeps every chain finite while that refusal is reported.
    if parent is not None and t.merge(node_id) == Merge.PARENT:
        return parent
    return TOP + t.top(node_id)


def base_chain(t: Tree, node_id: str) -> list[str]:
    """The branches `node_id` builds on, nearest first, ending with its target's `TOP:`."""
    chain = [landing_target(t, node_id)]
    while not chain[-1].startswith(TOP):
        chain.append(landing_target(t, chain[-1]))
    return chain


def landing_chain(t: Tree, node_id: str) -> list[str]:
    """`node_id`, then each node whose branch carries its code on, up to the first that lands
    on its target."""
    chain = [node_id]
    while not (target := landing_target(t, chain[-1])).startswith(TOP):
        chain.append(target)
    return chain


def target(t: Tree, node_id: str) -> str:
    """The branch `node_id`'s code reaches at the top of its chain."""
    return base_chain(t, node_id)[-1].removeprefix(TOP)


def meet(t: Tree, bases: list[str], landings: list[str]) -> str:
    """`meeting` over chains already walked. Within one target the last of `landings` lands on
    the last of `bases`, so a node always meets; across targets, which a write refuses, the node
    landing the code on its own target stands in."""
    return next((z for z in landings if landing_target(t, z) in bases), landings[-1])


def meeting(t: Tree, x: str, y: str) -> str:
    """The node whose landing puts `y`'s code on a branch `x` builds on."""
    return meet(t, base_chain(t, x), landing_chain(t, y))


def satisfied(t: Tree, x: str, y: str) -> bool:
    status = t.status(y)
    if isinstance(status, DecisionStatus):
        return status != DecisionStatus.OPEN
    return status == Status.SUPERSEDED or t.on_target(meeting(t, x, y))


def sync_pairs(t: Tree, x: str, y: str) -> list[tuple[str, str]]:
    """The merges that carry `y`'s landed code down to `x`'s own base, as (source, base) pairs,
    top-down: each base is brought up to date from the one it lands on. None across targets:
    `y`'s code never reaches a branch `x` builds on."""
    bases = base_chain(t, x)
    met = landing_target(t, meeting(t, x, y))
    if met not in bases:
        return []
    reached = bases.index(met)
    return [(bases[j + 1], bases[j]) for j in range(reached - 1, -1, -1)]
