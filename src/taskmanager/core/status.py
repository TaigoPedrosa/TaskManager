from enum import StrEnum


class Status(StrEnum):
    READY = "READY"
    IMPLEMENTING = "IMPLEMENTING"
    IMPLEMENTED = "IMPLEMENTED"
    REVIEWING = "REVIEWING"
    REVIEWED = "REVIEWED"
    FIXING = "FIXING"
    FIXED = "FIXED"
    MERGING = "MERGING"
    LANDED = "LANDED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    DEFERRED = "DEFERRED"
    ABANDONED = "ABANDONED"
    SUPERSEDED = "SUPERSEDED"


IN_STEP: frozenset[Status] = frozenset(
    {Status.IMPLEMENTING, Status.REVIEWING, Status.FIXING, Status.MERGING}
)
EXITS: frozenset[Status] = frozenset({Status.DEFERRED, Status.ABANDONED, Status.SUPERSEDED})
STABLE: frozenset[Status] = frozenset(
    {
        Status.READY,
        Status.IMPLEMENTED,
        Status.REVIEWED,
        Status.FIXED,
        Status.LANDED,
        Status.COMPLETED,
        Status.FAILED,
    }
)
# A set-aside child can never complete, so a container's rollup counts it on neither side.
SET_ASIDE = EXITS


class DecisionStatus(StrEnum):
    OPEN = "OPEN"
    ANSWERED = "ANSWERED"
    WITHDRAWN = "WITHDRAWN"


class Outcome(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"
    MERGE_FAILED = "merge_failed"


class Merge(StrEnum):
    PARENT = "parent"
    MAIN = "main"


class Phase(StrEnum):
    QUEUED = "QUEUED"
    DISPATCHED = "DISPATCHED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    DEFERRED = "DEFERRED"
    ABANDONED = "ABANDONED"
    SUPERSEDED = "SUPERSEDED"


class DisplayStatus(StrEnum):
    READY = "READY"
    IMPLEMENTING = "IMPLEMENTING"
    REVIEWING = "REVIEWING"
    FIXING = "FIXING"
    MERGING = "MERGING"
    COMPLETED = "COMPLETED"
    LANDED = "LANDED"
    FAILED = "FAILED"
    DEFERRED = "DEFERRED"
    ABANDONED = "ABANDONED"
    SUPERSEDED = "SUPERSEDED"
    WAITING_REVIEW = "WAITING_REVIEW"
    WAITING_FIX = "WAITING_FIX"
    WAITING_MERGE = "WAITING_MERGE"
    WAITING_MERGE_AGENT = "WAITING_MERGE_AGENT"
    STALE = "STALE"
    AWAITING_DECISION = "AWAITING_DECISION"
    BLOCKED_BY_TASK = "BLOCKED_BY_TASK"
    BLOCKED_BY_CONDITION = "BLOCKED_BY_CONDITION"
    BLOCKED_BY_SYNC = "BLOCKED_BY_SYNC"
    BLOCKED_BY_LEASE = "BLOCKED_BY_LEASE"


class Action(StrEnum):
    IMPLEMENT = "implement"
    REVIEW = "review"
    FIX = "fix"
    MERGE = "merge"
    SYNC = "sync"
    BLOCKED = "blocked"


class Event(StrEnum):
    COMPLETE = "complete"
    APPROVE = "approve"
    REJECT = "reject"
    LANDED = "landed"
    OWN_DEFECT = "own_defect"
    RELEASE = "release"
    RELEASE_BLOCKED = "release_blocked"
    EXPIRED = "expired"


class JobKind(StrEnum):
    LAND = "land"
    SYNC = "sync"


class JobState(StrEnum):
    RUNNING = "running"
    NEEDS_AGENT = "needs_agent"
    SUCCEEDED = "succeeded"
    OWN_DEFECT = "own_defect"
    CONDITION_UNMET = "condition_unmet"
    # The job's lease was swept or released before the job finished: a killed job must not
    # read as running forever and hold its node.
    EXPIRED = "expired"


class ConditionStage(StrEnum):
    CLAIM = "claim"
    LANDING = "landing"


class DecisionEffect(StrEnum):
    NONE = "none"
    ABANDON = "abandon"
    DEFER = "defer"
    REOPEN = "reopen"
    DROP_EDGE = "drop_edge"
