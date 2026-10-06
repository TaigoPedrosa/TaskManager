import itertools

import pytest

from taskmanager.core.lifecycle import (
    Caps,
    Cycle,
    LifecycleError,
    abandon,
    advance,
    claim,
    defer,
    fix_round,
    next_action,
    reopen,
    reset,
)
from taskmanager.core.status import IN_STEP, Action, Event, Outcome, Status

S = Status
APPROVE = Outcome.APPROVE
REJECT = Outcome.REJECT
MERGE_FAILED = Outcome.MERGE_FAILED
E = Event
CAPS = Caps()
NO_FIX = {"review": True, "fix": False}
UNREVIEWED = {"review": False, "fix": False}
SENSITIVE = {"sensitive": True}


@pytest.mark.parametrize(
    ("cycle", "action"),
    [
        (Cycle(S.READY), Action.IMPLEMENT),
        (Cycle(S.READY, container=True), None),
        (Cycle(S.IMPLEMENTED), Action.REVIEW),
        (Cycle(S.IMPLEMENTED, **UNREVIEWED), Action.MERGE),
        (Cycle(S.IMPLEMENTED, container=True), Action.MERGE),
        (Cycle(S.IMPLEMENTED, container=True, **UNREVIEWED), Action.MERGE),
        (Cycle(S.REVIEWED, outcome=APPROVE), Action.MERGE),
        (Cycle(S.REVIEWED, outcome=REJECT), Action.FIX),
        (Cycle(S.REVIEWED, outcome=REJECT, **NO_FIX), Action.MERGE),
        (Cycle(S.REVIEWED, outcome=MERGE_FAILED), Action.FIX),
        (Cycle(S.FIXED, fix_for=REJECT), Action.MERGE),
        (Cycle(S.FIXED, fix_for=MERGE_FAILED), Action.MERGE),
        (Cycle(S.FIXED, fix_for=REJECT, **SENSITIVE), Action.REVIEW),
        (Cycle(S.FIXED, fix_for=MERGE_FAILED, **SENSITIVE), Action.REVIEW),
        (Cycle(S.LANDED, container=True), Action.REVIEW),
        (Cycle(S.IMPLEMENTING), None),
        (Cycle(S.REVIEWING), None),
        (Cycle(S.FIXING), None),
        (Cycle(S.MERGING), None),
        (Cycle(S.COMPLETED), None),
        (Cycle(S.FAILED), None),
        (Cycle(S.DEFERRED), None),
        (Cycle(S.ABANDONED), None),
        (Cycle(S.SUPERSEDED), None),
    ],
)
def test_next_action_follows_the_status_and_flags(cycle: Cycle, action: Action | None) -> None:
    assert next_action(cycle) == action


def test_reviewed_without_an_outcome_is_refused_with_the_reset_fix() -> None:
    with pytest.raises(LifecycleError, match="--outcome"):
        next_action(Cycle(S.REVIEWED))


@pytest.mark.parametrize(
    ("before", "after"),
    [
        (Cycle(S.READY), Cycle(S.IMPLEMENTING, claimed_from=S.READY)),
        (
            Cycle(S.IMPLEMENTED),
            Cycle(S.REVIEWING, claimed_from=S.IMPLEMENTED, review_cycles=1),
        ),
        (
            Cycle(S.IMPLEMENTED, **UNREVIEWED),
            Cycle(S.MERGING, claimed_from=S.IMPLEMENTED, **UNREVIEWED),
        ),
        (
            Cycle(S.IMPLEMENTED, container=True),
            Cycle(S.MERGING, container=True, claimed_from=S.IMPLEMENTED),
        ),
        (
            Cycle(S.LANDED, container=True),
            Cycle(S.REVIEWING, container=True, claimed_from=S.LANDED, review_cycles=1),
        ),
        (
            Cycle(S.REVIEWED, outcome=REJECT, review_cycles=1),
            Cycle(
                S.FIXING,
                outcome=REJECT,
                fix_for=REJECT,
                claimed_from=S.REVIEWED,
                review_cycles=1,
            ),
        ),
        (
            Cycle(S.REVIEWED, outcome=MERGE_FAILED, review_cycles=1, merge_attempts=1),
            Cycle(
                S.FIXING,
                outcome=MERGE_FAILED,
                fix_for=MERGE_FAILED,
                claimed_from=S.REVIEWED,
                review_cycles=1,
                merge_attempts=1,
            ),
        ),
        (
            Cycle(S.REVIEWED, outcome=REJECT, review_cycles=1, **NO_FIX),
            Cycle(
                S.MERGING,
                outcome=REJECT,
                claimed_from=S.REVIEWED,
                review_cycles=1,
                **NO_FIX,
            ),
        ),
        (
            Cycle(S.REVIEWED, outcome=APPROVE, review_cycles=1),
            Cycle(S.MERGING, outcome=APPROVE, claimed_from=S.REVIEWED, review_cycles=1),
        ),
        (
            Cycle(S.FIXED, outcome=REJECT, fix_for=REJECT, review_cycles=1),
            Cycle(
                S.MERGING,
                outcome=REJECT,
                fix_for=REJECT,
                claimed_from=S.FIXED,
                review_cycles=1,
            ),
        ),
        (
            Cycle(S.FIXED, outcome=REJECT, fix_for=REJECT, review_cycles=1, **SENSITIVE),
            Cycle(
                S.REVIEWING,
                outcome=REJECT,
                fix_for=REJECT,
                claimed_from=S.FIXED,
                review_cycles=2,
                **SENSITIVE,
            ),
        ),
        (
            Cycle(
                S.FIXED, outcome=MERGE_FAILED, fix_for=MERGE_FAILED, review_cycles=1, **SENSITIVE
            ),
            Cycle(
                S.REVIEWING,
                outcome=MERGE_FAILED,
                fix_for=MERGE_FAILED,
                claimed_from=S.FIXED,
                review_cycles=1,
                **SENSITIVE,
            ),
        ),
    ],
)
def test_a_claim_enters_the_next_step_and_counts_only_review_rounds(
    before: Cycle, after: Cycle
) -> None:
    assert claim(before) == after


def test_a_claim_leaves_step_failures_to_the_step_that_ends() -> None:
    assert claim(Cycle(S.READY, step_failures=2)).step_failures == 2


@pytest.mark.parametrize(
    "cycle",
    [
        Cycle(S.READY, container=True),
        Cycle(S.IMPLEMENTING),
        Cycle(S.REVIEWING),
        Cycle(S.FIXING),
        Cycle(S.MERGING),
        Cycle(S.COMPLETED),
        Cycle(S.FAILED),
        Cycle(S.DEFERRED),
        Cycle(S.ABANDONED),
        Cycle(S.SUPERSEDED),
    ],
)
def test_a_claim_with_no_next_action_is_refused(cycle: Cycle) -> None:
    with pytest.raises(LifecycleError, match="no step to claim"):
        claim(cycle)


@pytest.mark.parametrize(
    ("before", "event", "after"),
    [
        (
            Cycle(S.IMPLEMENTING, claimed_from=S.READY, step_failures=2),
            E.COMPLETE,
            Cycle(S.IMPLEMENTED),
        ),
        (
            Cycle(S.FIXING, outcome=REJECT, fix_for=REJECT, claimed_from=S.REVIEWED),
            E.COMPLETE,
            Cycle(S.FIXED, outcome=REJECT, fix_for=REJECT),
        ),
        (
            Cycle(S.REVIEWING, claimed_from=S.IMPLEMENTED, review_cycles=1, step_failures=1),
            E.APPROVE,
            Cycle(S.REVIEWED, outcome=APPROVE, review_cycles=1),
        ),
        (
            Cycle(S.REVIEWING, claimed_from=S.IMPLEMENTED, review_cycles=1),
            E.REJECT,
            Cycle(S.REVIEWED, outcome=REJECT, review_cycles=1),
        ),
        (
            Cycle(S.REVIEWING, claimed_from=S.FIXED, fix_for=REJECT, review_cycles=2, **SENSITIVE),
            E.REJECT,
            Cycle(S.FAILED, outcome=REJECT, fix_for=REJECT, review_cycles=2, **SENSITIVE),
        ),
        (
            Cycle(
                S.REVIEWING,
                container=True,
                claimed_from=S.FIXED,
                fix_for=REJECT,
                review_cycles=2,
                **SENSITIVE,
            ),
            E.REJECT,
            Cycle(
                S.FAILED,
                container=True,
                outcome=REJECT,
                fix_for=REJECT,
                review_cycles=2,
                **SENSITIVE,
            ),
        ),
        (
            Cycle(S.REVIEWING, claimed_from=S.IMPLEMENTED, review_cycles=3),
            E.REJECT,
            Cycle(S.REVIEWED, outcome=REJECT, review_cycles=3),
        ),
        (
            Cycle(S.REVIEWING, claimed_from=S.IMPLEMENTED, review_cycles=3, **SENSITIVE),
            E.REJECT,
            Cycle(S.FAILED, outcome=REJECT, review_cycles=3, **SENSITIVE),
        ),
        (
            Cycle(S.REVIEWING, claimed_from=S.IMPLEMENTED, review_cycles=2, **SENSITIVE),
            E.REJECT,
            Cycle(S.REVIEWED, outcome=REJECT, review_cycles=2, **SENSITIVE),
        ),
        (
            Cycle(S.REVIEWING, container=True, claimed_from=S.LANDED, review_cycles=4),
            E.REJECT,
            Cycle(S.REVIEWED, container=True, outcome=REJECT, review_cycles=4),
        ),
        (
            Cycle(S.REVIEWING, container=True, claimed_from=S.LANDED, review_cycles=4, **SENSITIVE),
            E.REJECT,
            Cycle(S.FAILED, container=True, outcome=REJECT, review_cycles=4, **SENSITIVE),
        ),
        (
            Cycle(
                S.REVIEWING,
                claimed_from=S.IMPLEMENTED,
                review_cycles=5,
                **NO_FIX,
                **SENSITIVE,
            ),
            E.REJECT,
            Cycle(S.REVIEWED, outcome=REJECT, review_cycles=5, **NO_FIX, **SENSITIVE),
        ),
        (
            Cycle(S.REVIEWING, container=True, claimed_from=S.LANDED, review_cycles=1),
            E.APPROVE,
            Cycle(S.COMPLETED, container=True, outcome=APPROVE, review_cycles=1),
        ),
        (
            Cycle(S.REVIEWING, container=True, claimed_from=S.LANDED, review_cycles=1),
            E.REJECT,
            Cycle(S.REVIEWED, container=True, outcome=REJECT, review_cycles=1),
        ),
        (
            Cycle(S.REVIEWING, container=True, claimed_from=S.LANDED, review_cycles=1, **NO_FIX),
            E.REJECT,
            Cycle(S.FAILED, container=True, outcome=REJECT, review_cycles=1, **NO_FIX),
        ),
        (
            Cycle(S.MERGING, outcome=REJECT, claimed_from=S.REVIEWED, **NO_FIX),
            E.LANDED,
            Cycle(S.COMPLETED, outcome=REJECT, **NO_FIX),
        ),
        (
            Cycle(S.MERGING, container=True, claimed_from=S.IMPLEMENTED),
            E.LANDED,
            Cycle(S.LANDED, container=True),
        ),
        (
            Cycle(S.MERGING, container=True, claimed_from=S.IMPLEMENTED, review_cycles=2),
            E.LANDED,
            Cycle(S.LANDED, container=True, review_cycles=2),
        ),
        (
            Cycle(
                S.MERGING,
                container=True,
                outcome=MERGE_FAILED,
                fix_for=MERGE_FAILED,
                claimed_from=S.FIXED,
                merge_attempts=1,
            ),
            E.LANDED,
            Cycle(
                S.LANDED,
                container=True,
                outcome=MERGE_FAILED,
                fix_for=MERGE_FAILED,
                merge_attempts=1,
            ),
        ),
        (
            Cycle(
                S.MERGING,
                container=True,
                outcome=REJECT,
                fix_for=REJECT,
                claimed_from=S.FIXED,
                review_cycles=1,
            ),
            E.LANDED,
            Cycle(S.COMPLETED, container=True, outcome=REJECT, fix_for=REJECT, review_cycles=1),
        ),
        (
            Cycle(S.MERGING, container=True, claimed_from=S.IMPLEMENTED, **UNREVIEWED),
            E.LANDED,
            Cycle(S.COMPLETED, container=True, **UNREVIEWED),
        ),
        (
            Cycle(S.MERGING, outcome=APPROVE, claimed_from=S.REVIEWED),
            E.OWN_DEFECT,
            Cycle(S.REVIEWED, outcome=MERGE_FAILED, merge_attempts=1),
        ),
        (
            Cycle(S.MERGING, outcome=APPROVE, claimed_from=S.REVIEWED, merge_attempts=2),
            E.OWN_DEFECT,
            Cycle(S.REVIEWED, outcome=MERGE_FAILED, merge_attempts=3),
        ),
        (
            Cycle(S.MERGING, outcome=APPROVE, claimed_from=S.REVIEWED, merge_attempts=3),
            E.OWN_DEFECT,
            Cycle(S.FAILED, outcome=MERGE_FAILED, merge_attempts=4),
        ),
        (
            Cycle(S.MERGING, claimed_from=S.IMPLEMENTED, **UNREVIEWED),
            E.OWN_DEFECT,
            Cycle(S.FAILED, outcome=MERGE_FAILED, merge_attempts=1, **UNREVIEWED),
        ),
        (
            Cycle(S.IMPLEMENTING, claimed_from=S.READY),
            E.RELEASE,
            Cycle(S.READY, step_failures=1),
        ),
        (
            Cycle(S.REVIEWING, claimed_from=S.IMPLEMENTED, review_cycles=1),
            E.RELEASE,
            Cycle(S.IMPLEMENTED, review_cycles=0, step_failures=1),
        ),
        (
            Cycle(S.REVIEWING, claimed_from=S.FIXED, fix_for=REJECT, review_cycles=2),
            E.EXPIRED,
            Cycle(S.FIXED, fix_for=REJECT, review_cycles=1, step_failures=1),
        ),
        (
            Cycle(S.REVIEWING, container=True, claimed_from=S.LANDED, review_cycles=1),
            E.RELEASE,
            Cycle(S.LANDED, container=True, review_cycles=0, step_failures=1),
        ),
        (
            Cycle(
                S.REVIEWING,
                claimed_from=S.FIXED,
                fix_for=MERGE_FAILED,
                review_cycles=1,
            ),
            E.RELEASE,
            Cycle(S.FIXED, fix_for=MERGE_FAILED, review_cycles=1, step_failures=1),
        ),
        (
            Cycle(S.MERGING, outcome=APPROVE, claimed_from=S.REVIEWED),
            E.EXPIRED,
            Cycle(S.REVIEWED, outcome=APPROVE, step_failures=1),
        ),
        (
            Cycle(S.FIXING, outcome=REJECT, fix_for=REJECT, claimed_from=S.REVIEWED),
            E.RELEASE_BLOCKED,
            Cycle(S.REVIEWED, outcome=REJECT, fix_for=REJECT),
        ),
        (
            Cycle(S.REVIEWING, claimed_from=S.IMPLEMENTED, review_cycles=1, step_failures=1),
            E.RELEASE_BLOCKED,
            Cycle(S.IMPLEMENTED, review_cycles=0, step_failures=1),
        ),
        (
            Cycle(S.IMPLEMENTING, claimed_from=S.READY, step_failures=2),
            E.RELEASE,
            Cycle(S.FAILED, step_failures=3),
        ),
        (
            Cycle(S.MERGING, outcome=APPROVE, claimed_from=S.REVIEWED, step_failures=2),
            E.EXPIRED,
            Cycle(S.FAILED, outcome=APPROVE, step_failures=3),
        ),
        (Cycle(S.READY), E.RELEASE, Cycle(S.READY, step_failures=1)),
        (
            Cycle(S.FIXED, fix_for=REJECT, review_cycles=2),
            E.EXPIRED,
            Cycle(S.FIXED, fix_for=REJECT, review_cycles=2, step_failures=1),
        ),
        (
            Cycle(S.REVIEWED, outcome=APPROVE, step_failures=2),
            E.EXPIRED,
            Cycle(S.FAILED, outcome=APPROVE, step_failures=3),
        ),
        (
            Cycle(S.IMPLEMENTED, step_failures=1),
            E.RELEASE_BLOCKED,
            Cycle(S.IMPLEMENTED, step_failures=1),
        ),
    ],
)
def test_an_event_moves_the_node_along_its_row(before: Cycle, event: Event, after: Cycle) -> None:
    assert advance(before, event, CAPS) == after


_ACCEPTED = {
    (S.IMPLEMENTING, E.COMPLETE),
    (S.FIXING, E.COMPLETE),
    (S.REVIEWING, E.APPROVE),
    (S.REVIEWING, E.REJECT),
    (S.MERGING, E.LANDED),
    (S.MERGING, E.OWN_DEFECT),
    *itertools.product(
        IN_STEP | {S.READY, S.IMPLEMENTED, S.REVIEWED, S.FIXED},
        (E.RELEASE, E.RELEASE_BLOCKED, E.EXPIRED),
    ),
}


@pytest.mark.parametrize(
    ("status", "event"),
    [pair for pair in itertools.product(Status, Event) if pair not in _ACCEPTED],
)
def test_an_event_its_status_has_no_row_for_is_refused(status: Status, event: Event) -> None:
    claimed_from = S.READY if status in IN_STEP else None
    with pytest.raises(LifecycleError, match="does not accept"):
        advance(Cycle(status, claimed_from=claimed_from), event, CAPS)


def test_a_release_with_no_claimed_from_is_refused_with_the_reimport_fix() -> None:
    # A reset refuses a node in a step, so the message must not send anyone to it.
    with pytest.raises(LifecycleError, match="re-import") as refused:
        advance(Cycle(S.IMPLEMENTING), E.RELEASE, CAPS)
    assert "reset" not in str(refused.value)


def test_a_rejected_review_of_a_landing_fix_fails_and_counts_no_round() -> None:
    reviewing = Cycle(
        S.REVIEWING,
        outcome=MERGE_FAILED,
        fix_for=MERGE_FAILED,
        claimed_from=S.FIXED,
        review_cycles=1,
        **SENSITIVE,
    )
    rejected = advance(reviewing, E.REJECT, CAPS)
    assert (rejected.status, rejected.outcome, rejected.review_cycles) == (S.FAILED, REJECT, 1)


def _walk(cycle: Cycle, *steps: Event | None) -> Cycle:
    for step in steps:
        cycle = claim(cycle) if step is None else advance(cycle, step, CAPS)
    return cycle


# Claim the step, finish it, claim the review that follows, reject.
REJECTED_ROUND = (None, E.COMPLETE, None, E.REJECT)


def test_a_rejected_task_lands_its_one_fix_without_a_second_review() -> None:
    fixed = _walk(Cycle(S.READY), *REJECTED_ROUND, None, E.COMPLETE)
    assert (fixed.status, next_action(fixed)) == (S.FIXED, Action.MERGE)
    assert _walk(fixed, None, E.LANDED).status == S.COMPLETED


def test_a_sensitive_task_fails_when_the_one_review_of_its_fix_rejects() -> None:
    fixed = _walk(Cycle(S.READY, **SENSITIVE), *REJECTED_ROUND, None, E.COMPLETE)
    assert (fixed.status, next_action(fixed)) == (S.FIXED, Action.REVIEW)
    assert _walk(fixed, None, E.REJECT).status == S.FAILED


def test_a_reviewed_container_lands_before_its_one_review_and_completes_on_approval() -> None:
    landed = _walk(Cycle(S.IMPLEMENTED, container=True), None, E.LANDED)
    assert (landed.status, next_action(landed)) == (S.LANDED, Action.REVIEW)
    assert _walk(landed, None, E.APPROVE).status == S.COMPLETED


def test_a_rejected_landed_container_lands_its_fix_and_completes_without_a_second_review() -> None:
    landed = Cycle(S.IMPLEMENTED, container=True)
    fixed = _walk(landed, None, E.LANDED, None, E.REJECT, None, E.COMPLETE)
    assert (fixed.status, next_action(fixed)) == (S.FIXED, Action.MERGE)
    assert _walk(fixed, None, E.LANDED).status == S.COMPLETED


def test_a_container_whose_landing_was_fixed_still_owes_its_one_review() -> None:
    fixed = _walk(Cycle(S.IMPLEMENTED, container=True), None, E.OWN_DEFECT, None, E.COMPLETE)
    assert _walk(fixed, None, E.LANDED).status == S.LANDED


def test_a_review_of_a_landing_fix_uses_no_fix_round() -> None:
    approved = _walk(Cycle(S.READY, **SENSITIVE), None, E.COMPLETE, None, E.APPROVE)
    reviewing = _walk(approved, None, E.OWN_DEFECT, None, E.COMPLETE, None)
    assert (reviewing.status, reviewing.review_cycles) == (S.REVIEWING, 1)


def test_landing_fixes_stop_after_the_attempt_cap() -> None:
    approved = _walk(Cycle(S.READY), None, E.COMPLETE, None, E.APPROVE)
    landing_fix = (None, E.OWN_DEFECT, None, E.COMPLETE)
    three_fixed = _walk(approved, *landing_fix, *landing_fix, *landing_fix)
    assert (three_fixed.status, three_fixed.merge_attempts) == (S.FIXED, 3)
    failed = _walk(three_fixed, None, E.OWN_DEFECT)
    assert (failed.status, failed.merge_attempts) == (S.FAILED, 4)


def test_step_failures_reset_when_a_step_makes_progress() -> None:
    twice_released = _walk(Cycle(S.READY), None, E.RELEASE, None, E.EXPIRED)
    assert twice_released.step_failures == 2
    assert _walk(twice_released, None, E.COMPLETE).step_failures == 0


@pytest.mark.parametrize(
    ("cycle", "round_"),
    [
        (Cycle(S.FIXING, fix_for=REJECT, review_cycles=1), 1),
        (Cycle(S.FIXING, fix_for=REJECT, review_cycles=2), 2),
        (Cycle(S.FIXING, container=True, fix_for=REJECT, review_cycles=3), 3),
        (Cycle(S.FIXING, fix_for=MERGE_FAILED, review_cycles=2), 0),
        (Cycle(S.REVIEWED, outcome=REJECT, review_cycles=2), 2),
        (Cycle(S.REVIEWED, outcome=MERGE_FAILED, review_cycles=2), 0),
    ],
)
def test_fix_round_is_the_review_round_the_fix_answers(cycle: Cycle, round_: int) -> None:
    assert fix_round(cycle) == round_


@pytest.mark.parametrize(
    ("cycle", "children_all_completed", "status"),
    [
        (Cycle(S.FAILED), False, S.READY),
        (Cycle(S.DEFERRED), False, S.READY),
        (Cycle(S.ABANDONED), True, S.READY),
        (Cycle(S.FAILED, container=True), True, S.IMPLEMENTED),
        (Cycle(S.DEFERRED, container=True), False, S.READY),
    ],
)
def test_reopen_restarts_the_cycle_with_cleared_counters(
    cycle: Cycle, children_all_completed: bool, status: Status
) -> None:
    worn = Cycle(
        cycle.status,
        container=cycle.container,
        fix=False,
        sensitive=True,
        outcome=REJECT,
        fix_for=REJECT,
        review_cycles=3,
        merge_attempts=2,
        step_failures=1,
    )
    assert reopen(worn, children_all_completed) == Cycle(
        status, container=cycle.container, fix=False, sensitive=True
    )


@pytest.mark.parametrize(
    "status",
    [s for s in Status if s not in (S.FAILED, S.DEFERRED, S.ABANDONED)],
)
def test_reopen_is_refused_outside_failed_deferred_and_abandoned(status: Status) -> None:
    with pytest.raises(LifecycleError, match="reopen"):
        reopen(Cycle(status), children_all_completed=False)


@pytest.mark.parametrize("status", [S.READY, S.IMPLEMENTED, S.REVIEWED, S.FIXED, S.FAILED])
def test_defer_and_abandon_accept_every_stable_status_but_completed(status: Status) -> None:
    assert defer(Cycle(status)).status == S.DEFERRED
    assert abandon(Cycle(status)).status == S.ABANDONED


def test_defer_and_abandon_refuse_landed_code() -> None:
    for verb in (defer, abandon):
        with pytest.raises(LifecycleError, match="landed code"):
            verb(Cycle(S.COMPLETED))


@pytest.mark.parametrize("status", [S.IMPLEMENTING, S.REVIEWING, S.FIXING, S.MERGING])
def test_defer_and_abandon_refuse_a_node_in_a_step(status: Status) -> None:
    for verb in (defer, abandon):
        with pytest.raises(LifecycleError, match="wait for its step to end"):
            verb(Cycle(status, claimed_from=S.READY))


@pytest.mark.parametrize(
    ("status", "fix"),
    [
        (S.DEFERRED, "already DEFERRED; reopen it first"),
        (S.ABANDONED, "already ABANDONED; reopen it first"),
        (S.SUPERSEDED, "already SUPERSEDED; its replacement carries the work"),
    ],
)
def test_defer_and_abandon_refuse_a_node_already_set_aside_and_say_what_to_do(
    status: Status, fix: str
) -> None:
    for verb in (defer, abandon):
        with pytest.raises(LifecycleError, match=fix):
            verb(Cycle(status))


@pytest.mark.parametrize(
    ("to", "outcome", "after"),
    [
        (S.READY, None, Cycle(S.READY, review_cycles=2)),
        (S.IMPLEMENTED, None, Cycle(S.IMPLEMENTED, review_cycles=2)),
        (S.REVIEWED, REJECT, Cycle(S.REVIEWED, outcome=REJECT, review_cycles=2)),
        (
            S.FIXED,
            MERGE_FAILED,
            Cycle(S.FIXED, outcome=MERGE_FAILED, fix_for=MERGE_FAILED, review_cycles=2),
        ),
        (S.LANDED, None, Cycle(S.LANDED, review_cycles=2)),
        (S.COMPLETED, None, Cycle(S.COMPLETED, review_cycles=2)),
    ],
)
def test_reset_moves_a_stable_node_and_keeps_its_review_count(
    to: Status, outcome: Outcome | None, after: Cycle
) -> None:
    failed = Cycle(S.FAILED, outcome=APPROVE, review_cycles=2, step_failures=3)
    assert reset(failed, to, outcome) == after


@pytest.mark.parametrize(
    ("cycle", "to", "outcome", "message"),
    [
        (Cycle(S.REVIEWING), S.READY, None, "in a step"),
        (Cycle(S.FAILED), S.MERGING, None, "reset goes to"),
        (Cycle(S.FAILED), S.DEFERRED, None, "reset goes to"),
        (Cycle(S.FAILED), S.REVIEWED, None, "needs --outcome"),
        (Cycle(S.FAILED), S.FIXED, None, "needs --outcome"),
        (Cycle(S.FAILED), S.READY, REJECT, "applies to"),
        (Cycle(S.FAILED), S.LANDED, REJECT, "applies to"),
        (Cycle(S.FAILED), S.FIXED, APPROVE, "never approve"),
        (Cycle(S.FAILED, **NO_FIX), S.FIXED, REJECT, "fix off"),
        (Cycle(S.FAILED, **NO_FIX), S.REVIEWED, MERGE_FAILED, "fix off"),
    ],
)
def test_reset_refuses_a_target_the_cycle_cannot_route(
    cycle: Cycle, to: Status, outcome: Outcome | None, message: str
) -> None:
    with pytest.raises(LifecycleError, match=message):
        reset(cycle, to, outcome)
