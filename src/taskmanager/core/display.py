from dataclasses import dataclass
from typing import Literal

from taskmanager.core.lifecycle import Cycle, next_action
from taskmanager.core.status import EXITS, IN_STEP, Action, DisplayStatus, Phase, Status


@dataclass(frozen=True)
class Facts:
    lease: Literal["live", "expired", "none"] = "none"
    job_needs_agent: bool = False
    open_decision: bool = False
    unsatisfied_edge: bool = False
    unmet_condition: bool = False
    sync_pending: bool = False
    files_locked: bool = False
    descendant_started: bool = False


_WAITING = {
    Action.REVIEW: DisplayStatus.WAITING_REVIEW,
    Action.FIX: DisplayStatus.WAITING_FIX,
    Action.MERGE: DisplayStatus.WAITING_MERGE,
}


def phase(status: Status) -> Phase:
    if status == Status.READY:
        return Phase.QUEUED
    if status in EXITS or status in (Status.COMPLETED, Status.FAILED):
        return Phase(status.value)
    return Phase.DISPATCHED


def display_status(c: Cycle, f: Facts) -> DisplayStatus:
    """The one status a reader sees; the first matching condition wins."""
    if c.status in EXITS or c.status in (Status.COMPLETED, Status.FAILED):
        return DisplayStatus(c.status.value)
    if c.status == Status.MERGING and f.job_needs_agent:
        return DisplayStatus.WAITING_MERGE_AGENT
    if c.status in IN_STEP:
        return DisplayStatus(c.status.value) if f.lease == "live" else DisplayStatus.STALE
    for holds, shown in (
        (f.open_decision, DisplayStatus.AWAITING_DECISION),
        (f.unsatisfied_edge, DisplayStatus.BLOCKED_BY_TASK),
        (f.unmet_condition, DisplayStatus.BLOCKED_BY_CONDITION),
        (f.sync_pending, DisplayStatus.BLOCKED_BY_SYNC),
        (f.files_locked, DisplayStatus.BLOCKED_BY_LEASE),
    ):
        if holds:
            return shown
    if c.status == Status.READY:
        started = c.container and f.descendant_started
        return DisplayStatus.IMPLEMENTING if started else DisplayStatus.READY
    action = next_action(c)
    assert action is not None, f"{c.status} is stable and not READY, so it has a next step"
    return _WAITING[action]
