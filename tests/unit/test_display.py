import pytest

from taskmanager.core.display import Facts, display_status, phase
from taskmanager.core.lifecycle import Cycle
from taskmanager.core.status import DisplayStatus, Outcome, Phase, Status

S = Status
D = DisplayStatus
LIVE = Facts(lease="live")
EVERY_BLOCKER = Facts(
    open_decision=True,
    unsatisfied_edge=True,
    unmet_condition=True,
    sync_pending=True,
    files_locked=True,
)


@pytest.mark.parametrize(
    ("status", "shown"),
    [
        (S.READY, Phase.QUEUED),
        (S.IMPLEMENTING, Phase.DISPATCHED),
        (S.IMPLEMENTED, Phase.DISPATCHED),
        (S.REVIEWING, Phase.DISPATCHED),
        (S.REVIEWED, Phase.DISPATCHED),
        (S.FIXING, Phase.DISPATCHED),
        (S.FIXED, Phase.DISPATCHED),
        (S.MERGING, Phase.DISPATCHED),
        (S.COMPLETED, Phase.COMPLETED),
        (S.FAILED, Phase.FAILED),
        (S.DEFERRED, Phase.DEFERRED),
        (S.ABANDONED, Phase.ABANDONED),
        (S.SUPERSEDED, Phase.SUPERSEDED),
    ],
)
def test_phase_groups_every_step_between_ready_and_completed(status: Status, shown: Phase) -> None:
    assert phase(status) == shown


ROWS = [
    ("exit shows itself", Cycle(S.DEFERRED), EVERY_BLOCKER, D.DEFERRED),
    ("abandoned shows itself", Cycle(S.ABANDONED), Facts(), D.ABANDONED),
    ("superseded shows itself", Cycle(S.SUPERSEDED), Facts(), D.SUPERSEDED),
    ("completed shows itself", Cycle(S.COMPLETED), EVERY_BLOCKER, D.COMPLETED),
    ("failed shows itself", Cycle(S.FAILED), Facts(open_decision=True), D.FAILED),
    (
        "stopped landing waits for an agent",
        Cycle(S.MERGING),
        Facts(lease="live", job_needs_agent=True),
        D.WAITING_MERGE_AGENT,
    ),
    ("live implement", Cycle(S.IMPLEMENTING), LIVE, D.IMPLEMENTING),
    ("live review", Cycle(S.REVIEWING), LIVE, D.REVIEWING),
    ("live fix", Cycle(S.FIXING), LIVE, D.FIXING),
    ("live landing", Cycle(S.MERGING), LIVE, D.MERGING),
    (
        "a live step outranks every blocker",
        Cycle(S.IMPLEMENTING),
        Facts(
            lease="live",
            open_decision=True,
            unsatisfied_edge=True,
            unmet_condition=True,
            sync_pending=True,
            files_locked=True,
        ),
        D.IMPLEMENTING,
    ),
    ("expired lease", Cycle(S.REVIEWING), Facts(lease="expired"), D.STALE),
    ("no lease row", Cycle(S.FIXING), Facts(lease="none"), D.STALE),
    ("stale outranks a decision", Cycle(S.MERGING), Facts(open_decision=True), D.STALE),
    ("open decision", Cycle(S.READY), EVERY_BLOCKER, D.AWAITING_DECISION),
    (
        "unsatisfied edge",
        Cycle(S.IMPLEMENTED),
        Facts(unsatisfied_edge=True, unmet_condition=True, sync_pending=True, files_locked=True),
        D.BLOCKED_BY_TASK,
    ),
    (
        "unmet condition",
        Cycle(S.REVIEWED, outcome=Outcome.APPROVE),
        Facts(unmet_condition=True, sync_pending=True, files_locked=True),
        D.BLOCKED_BY_CONDITION,
    ),
    (
        "sync pending",
        Cycle(S.READY),
        Facts(sync_pending=True, files_locked=True),
        D.BLOCKED_BY_SYNC,
    ),
    ("files locked", Cycle(S.FIXED), Facts(files_locked=True), D.BLOCKED_BY_LEASE),
    (
        "blocked outranks a started container",
        Cycle(S.READY, container=True),
        Facts(unsatisfied_edge=True, descendant_started=True),
        D.BLOCKED_BY_TASK,
    ),
    (
        "container with a started descendant",
        Cycle(S.READY, container=True),
        Facts(descendant_started=True),
        D.IMPLEMENTING,
    ),
    ("container nothing started", Cycle(S.READY, container=True), Facts(), D.READY),
    (
        "a task ignores descendant_started",
        Cycle(S.READY),
        Facts(descendant_started=True),
        D.READY,
    ),
    ("ready", Cycle(S.READY), Facts(), D.READY),
    ("implemented with review", Cycle(S.IMPLEMENTED), Facts(), D.WAITING_REVIEW),
    (
        "implemented without review",
        Cycle(S.IMPLEMENTED, review=False, fix=False),
        Facts(),
        D.WAITING_MERGE,
    ),
    (
        "approved",
        Cycle(S.REVIEWED, outcome=Outcome.APPROVE),
        Facts(),
        D.WAITING_MERGE,
    ),
    (
        "rejected with fix on",
        Cycle(S.REVIEWED, outcome=Outcome.REJECT),
        Facts(),
        D.WAITING_FIX,
    ),
    (
        "landing failed",
        Cycle(S.REVIEWED, outcome=Outcome.MERGE_FAILED),
        Facts(),
        D.WAITING_FIX,
    ),
    (
        "rejected with fix off",
        Cycle(S.REVIEWED, fix=False, outcome=Outcome.REJECT),
        Facts(),
        D.WAITING_MERGE,
    ),
    ("fixed", Cycle(S.FIXED, fix_for=Outcome.REJECT), Facts(), D.WAITING_REVIEW),
]


@pytest.mark.parametrize(
    ("cycle", "facts", "shown"), [row[1:] for row in ROWS], ids=[row[0] for row in ROWS]
)
def test_the_first_matching_display_row_wins(
    cycle: Cycle, facts: Facts, shown: DisplayStatus
) -> None:
    assert display_status(cycle, facts) == shown


def test_every_display_status_is_reachable() -> None:
    assert {row[3] for row in ROWS} == set(DisplayStatus)
