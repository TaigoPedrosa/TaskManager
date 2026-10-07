"""Where a node lands and what it builds on, decided from stored statuses and the tree alone:
no git call and no repository comparison, so satisfaction is the same in every repository."""

from typing import Final, Protocol

from taskmanager.core.status import DecisionStatus, Merge, Status

MAIN: Final = "MAIN"
# Code on its landing target: completed, or landed with its one review still owed.
ON_TARGET: Final = frozenset({Status.LANDED, Status.COMPLETED})


class Tree(Protocol):
    def parent(self, node_id: str) -> str | None: ...
    def merge(self, node_id: str) -> Merge: ...
    def status(self, node_id: str) -> Status | DecisionStatus: ...


def landing_target(t: Tree, node_id: str) -> str:
    parent = t.parent(node_id)
    # A parentless node with merge=parent is refused at write time; reading it as landing on
    # main keeps every chain finite while that refusal is reported.
    return parent if parent is not None and t.merge(node_id) == Merge.PARENT else MAIN


def base_chain(t: Tree, node_id: str) -> list[str]:
    """The branches `node_id` builds on, nearest first, ending with MAIN."""
    chain = [landing_target(t, node_id)]
    while chain[-1] != MAIN:
        chain.append(landing_target(t, chain[-1]))
    return chain


def landing_chain(t: Tree, node_id: str) -> list[str]:
    """`node_id`, then each node whose branch carries its code on, up to the first that lands
    on MAIN."""
    chain = [node_id]
    while (target := landing_target(t, chain[-1])) != MAIN:
        chain.append(target)
    return chain


def meeting(t: Tree, x: str, y: str) -> str:
    """The node whose landing puts `y`'s code on a branch `x` builds on. It always exists,
    because the last node of every landing chain lands on MAIN and every base chain ends there."""
    bases = base_chain(t, x)
    return next(z for z in landing_chain(t, y) if landing_target(t, z) in bases)


def satisfied(t: Tree, x: str, y: str) -> bool:
    status = t.status(y)
    if isinstance(status, DecisionStatus):
        return status != DecisionStatus.OPEN
    return status == Status.SUPERSEDED or t.status(meeting(t, x, y)) in ON_TARGET


def sync_pairs(t: Tree, x: str, y: str) -> list[tuple[str, str]]:
    """The merges that carry `y`'s landed code down to `x`'s own base, as (source, base) pairs,
    top-down: each base is brought up to date from the one it lands on."""
    bases = base_chain(t, x)
    reached = bases.index(landing_target(t, meeting(t, x, y)))
    return [(bases[j + 1], bases[j]) for j in range(reached - 1, -1, -1)]
