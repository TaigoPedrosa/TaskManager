from dataclasses import dataclass, replace

from taskmanager.core.status import EXITS, IN_STEP, STABLE, Action, Event, Outcome, Status


@dataclass(frozen=True)
class Cycle:
    status: Status
    container: bool = False
    review: bool = True
    fix: bool = True
    sensitive: bool = False
    outcome: Outcome | None = None
    fix_for: Outcome | None = None
    claimed_from: Status | None = None
    review_cycles: int = 0
    merge_attempts: int = 0
    step_failures: int = 0


@dataclass(frozen=True)
class Caps:
    fix_rounds_task: int = 2
    fix_rounds_container: int = 3
    merge_attempts: int = 3
    step_failures: int = 3


class LifecycleError(ValueError):
    """A transition the stored state does not allow; the message names the state and the fix."""


_STEP_OF = {
    Action.IMPLEMENT: Status.IMPLEMENTING,
    Action.REVIEW: Status.REVIEWING,
    Action.FIX: Status.FIXING,
    Action.MERGE: Status.MERGING,
}
REOPENABLE = frozenset({Status.FAILED, Status.DEFERRED, Status.ABANDONED})
# Stable statuses a job's agent can still hold a lease over without a claim: a sync job runs
# for a node its claim left where it was. A LANDED node's code is already on its target, so
# nothing syncs or lands it.
_UNCLAIMED_WITH_A_JOB = STABLE - {Status.COMPLETED, Status.FAILED, Status.LANDED}
_RESET_TARGETS = frozenset(
    {
        Status.READY,
        Status.IMPLEMENTED,
        Status.REVIEWED,
        Status.FIXED,
        Status.LANDED,
        Status.COMPLETED,
    }
)


def next_action(c: Cycle) -> Action | None:
    """The step a claim on `c` starts. None when no claim can start one: COMPLETED, FAILED, an
    exit, a container at READY (its implement step is its children's work), and a node already
    in a step, whose lease or job is what an agent takes over."""
    match c.status:
        case Status.READY:
            return None if c.container else Action.IMPLEMENT
        case Status.IMPLEMENTED:
            # A container lands first; its one review reads the landed target.
            return Action.REVIEW if c.review and not c.container else Action.MERGE
        case Status.REVIEWED:
            return _after_review(c)
        case Status.FIXED:
            # Only a sensitive fix is re-reviewed; every other fix lands as it is.
            return Action.REVIEW if c.sensitive else Action.MERGE
        case Status.LANDED:
            # A landing completes a node with review off; nothing reviews one stored at LANDED.
            return Action.REVIEW if c.review else None
        case _:
            return None


def _after_review(c: Cycle) -> Action:
    if c.outcome == Outcome.MERGE_FAILED or (c.outcome == Outcome.REJECT and c.fix):
        return Action.FIX
    if c.outcome is None:
        raise LifecycleError("REVIEWED with no outcome: reset the node with --outcome")
    # A rejection nobody below fixes still lands, on a parent whose own review will see it.
    return Action.MERGE


def _counted(c: Cycle) -> bool:
    # A review after a landing fix checks that fix; it is not another review round.
    return c.claimed_from in (Status.IMPLEMENTED, Status.LANDED) or (
        c.claimed_from == Status.FIXED and c.fix_for == Outcome.REJECT
    )


def _review_owed(c: Cycle) -> bool:
    # A landing straight from IMPLEMENTED is always ahead of its review, whatever count a reset
    # kept; a landing after a landing fix is ahead of it only while no review has run.
    return (
        c.container and c.review and (c.claimed_from == Status.IMPLEMENTED or c.review_cycles == 0)
    )


def claim(c: Cycle) -> Cycle:
    action = next_action(c)
    if action is None:
        raise LifecycleError(f"a node at {c.status} has no step to claim")
    claimed = replace(c, status=_STEP_OF[action], claimed_from=c.status)
    if action == Action.FIX:
        return replace(claimed, fix_for=c.outcome)
    if action == Action.REVIEW and _counted(claimed):
        return replace(claimed, review_cycles=c.review_cycles + 1)
    return claimed


def _progress(
    c: Cycle,
    status: Status,
    outcome: Outcome | None = None,
    merge_attempts: int | None = None,
) -> Cycle:
    return replace(
        c,
        status=status,
        claimed_from=None,
        step_failures=0,
        outcome=c.outcome if outcome is None else outcome,
        merge_attempts=c.merge_attempts if merge_attempts is None else merge_attempts,
    )


def _rejected(c: Cycle, caps: Caps) -> Cycle:
    # A review of a fix rejects to the owner, never into a second fix; and landed code nobody
    # fixes has no parent review left to catch it.
    if c.claimed_from == Status.FIXED or (c.claimed_from == Status.LANDED and not c.fix):
        return _progress(c, Status.FAILED, Outcome.REJECT)
    cap = caps.fix_rounds_container if c.container else caps.fix_rounds_task
    # Every other node spends at most one fix, so the fix-round caps bound only a sensitive one.
    out_of_rounds = c.sensitive and c.fix and c.review_cycles - 1 >= cap
    return _progress(c, Status.FAILED if out_of_rounds else Status.REVIEWED, Outcome.REJECT)


def _landing_failed(c: Cycle, caps: Caps) -> Cycle:
    failed = not c.fix or c.merge_attempts >= caps.merge_attempts
    return _progress(
        c,
        Status.FAILED if failed else Status.REVIEWED,
        Outcome.MERGE_FAILED,
        c.merge_attempts + 1,
    )


def _without_progress(c: Cycle, caps: Caps, back_to: Status, counts_as_failure: bool) -> Cycle:
    step_failures = c.step_failures + (1 if counts_as_failure else 0)
    return replace(
        c,
        status=Status.FAILED if step_failures >= caps.step_failures else back_to,
        claimed_from=None,
        step_failures=step_failures,
    )


def _back(c: Cycle, caps: Caps, counts_as_failure: bool) -> Cycle:
    if c.claimed_from is None:
        raise LifecycleError(
            f"{c.status} has no claimed_from to return to: re-import the node at the status its "
            "step was claimed from"
        )
    # A review that never delivered a verdict must not use up a fix round.
    uncounted = 1 if c.status == Status.REVIEWING and _counted(c) else 0
    returned = _without_progress(c, caps, c.claimed_from, counts_as_failure)
    return replace(returned, review_cycles=c.review_cycles - uncounted)


def advance(c: Cycle, event: Event, caps: Caps) -> Cycle:
    match (c.status, event):
        case (Status.IMPLEMENTING, Event.COMPLETE):
            return _progress(c, Status.IMPLEMENTED)
        case (Status.FIXING, Event.COMPLETE):
            return _progress(c, Status.FIXED)
        case (Status.REVIEWING, Event.APPROVE):
            approved = Status.COMPLETED if c.claimed_from == Status.LANDED else Status.REVIEWED
            return _progress(c, approved, Outcome.APPROVE)
        case (Status.REVIEWING, Event.REJECT):
            return _rejected(c, caps)
        case (Status.MERGING, Event.LANDED):
            return _progress(c, Status.LANDED if _review_owed(c) else Status.COMPLETED)
        case (Status.MERGING, Event.OWN_DEFECT):
            return _landing_failed(c, caps)
        case (status, Event.RELEASE | Event.EXPIRED) if status in IN_STEP:
            return _back(c, caps, counts_as_failure=True)
        case (status, Event.RELEASE_BLOCKED) if status in IN_STEP:
            return _back(c, caps, counts_as_failure=False)
        case (status, Event.RELEASE | Event.EXPIRED) if status in _UNCLAIMED_WITH_A_JOB:
            return _without_progress(c, caps, c.status, counts_as_failure=True)
        case (status, Event.RELEASE_BLOCKED) if status in _UNCLAIMED_WITH_A_JOB:
            return c
    raise LifecycleError(f"a node at {c.status} does not accept {event}")


def fix_round(c: Cycle) -> int:
    """The 1-based review round the fix `c` is in, or would start, answers; 0 for a fix
    answering a landing failure, which is not a review round."""
    answers = c.fix_for if c.status == Status.FIXING else c.outcome
    return c.review_cycles if answers == Outcome.REJECT else 0


def reopen(c: Cycle, children_all_completed: bool) -> Cycle:
    if c.status not in REOPENABLE:
        raise LifecycleError(f"only FAILED, DEFERRED or ABANDONED reopen; this node is {c.status}")
    ready = Status.IMPLEMENTED if c.container and children_all_completed else Status.READY
    return Cycle(
        status=ready, container=c.container, review=c.review, fix=c.fix, sensitive=c.sensitive
    )


def _set_aside(c: Cycle, to: Status) -> Cycle:
    if c.status == Status.COMPLETED:
        raise LifecycleError(f"a COMPLETED node is landed code and cannot become {to}")
    if c.status == Status.SUPERSEDED:
        raise LifecycleError("this node is already SUPERSEDED; its replacement carries the work")
    if c.status in EXITS:
        raise LifecycleError(f"this node is already {c.status}; reopen it first")
    if c.status not in STABLE:
        raise LifecycleError(
            f"a node at {c.status} cannot become {to}: wait for its step to end, or stop it"
        )
    return replace(c, status=to, claimed_from=None)


def defer(c: Cycle) -> Cycle:
    return _set_aside(c, Status.DEFERRED)


def abandon(c: Cycle) -> Cycle:
    return _set_aside(c, Status.ABANDONED)


def reset(c: Cycle, to: Status, outcome: Outcome | None) -> Cycle:
    if c.status in IN_STEP:
        raise LifecycleError(
            f"a node at {c.status} is in a step: wait for it to end, or stop it, then reset"
        )
    if to not in _RESET_TARGETS:
        raise LifecycleError(
            f"reset goes to READY, IMPLEMENTED, REVIEWED, FIXED, LANDED or COMPLETED, not {to}"
        )
    if to in (Status.REVIEWED, Status.FIXED) and outcome is None:
        raise LifecycleError(f"a reset to {to} needs --outcome")
    if to not in (Status.REVIEWED, Status.FIXED) and outcome is not None:
        raise LifecycleError(f"--outcome applies to a reset to REVIEWED or FIXED, not {to}")
    if to == Status.FIXED and outcome == Outcome.APPROVE:
        raise LifecycleError("a fix answers reject or merge_failed, never approve")
    if not c.fix and (to == Status.FIXED or outcome == Outcome.MERGE_FAILED):
        raise LifecycleError("this node has fix off: nothing fixes it; turn fix on first")
    if not c.review and to == Status.LANDED:
        raise LifecycleError(
            "this node has review off: nothing reviews it at LANDED; reset it to COMPLETED, or "
            "turn review on first"
        )
    return replace(
        c,
        status=to,
        claimed_from=None,
        step_failures=0,
        outcome=outcome,
        fix_for=outcome if to == Status.FIXED else None,
    )
