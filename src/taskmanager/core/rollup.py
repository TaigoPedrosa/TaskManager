from collections.abc import Sequence

from taskmanager.core.status import EXITS, IN_STEP, SET_ASIDE, Status

# Owned by the container's own cycle or by an explicit verb, never re-derived from children:
# a step in flight, landed code, a failure awaiting its decision, and an exit someone chose.
_KEPT = IN_STEP | EXITS | {Status.COMPLETED, Status.FAILED}


def rollup(current: Status, children: Sequence[Status]) -> Status:
    """A container's stored status re-derived from its children's, in the same write as any
    change to them. The empty-diff completion needs git and is the caller's."""
    if current in _KEPT:
        return current
    if not children:
        return Status.READY
    counted = [child for child in children if child not in SET_ASIDE]
    if not counted:
        if all(child == Status.SUPERSEDED for child in children):
            return Status.COMPLETED
        return Status.DEFERRED if Status.DEFERRED in children else Status.ABANDONED
    if all(child == Status.COMPLETED for child in counted):
        return Status.IMPLEMENTED if current == Status.READY else current
    # A child back in play takes a container past READY back to READY: its review, if any,
    # would otherwise read a branch still missing that child's code.
    return Status.READY
