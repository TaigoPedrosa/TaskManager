from enum import StrEnum

import pytest

from taskmanager.core.status import (
    EXITS,
    IN_STEP,
    SET_ASIDE,
    STABLE,
    Action,
    ConditionStage,
    DecisionEffect,
    DecisionStatus,
    DisplayStatus,
    Event,
    JobKind,
    JobState,
    Merge,
    Outcome,
    Phase,
    Status,
)


@pytest.mark.parametrize(
    ("enum", "values"),
    [
        (
            Status,
            [
                "READY",
                "IMPLEMENTING",
                "IMPLEMENTED",
                "REVIEWING",
                "REVIEWED",
                "FIXING",
                "FIXED",
                "MERGING",
                "LANDED",
                "COMPLETED",
                "FAILED",
                "DEFERRED",
                "ABANDONED",
                "SUPERSEDED",
            ],
        ),
        (DecisionStatus, ["OPEN", "ANSWERED", "WITHDRAWN"]),
        (Outcome, ["approve", "reject", "merge_failed"]),
        (Merge, ["parent", "main"]),
        (
            Phase,
            ["QUEUED", "DISPATCHED", "COMPLETED", "FAILED", "DEFERRED", "ABANDONED", "SUPERSEDED"],
        ),
        (
            DisplayStatus,
            [
                "READY",
                "IMPLEMENTING",
                "REVIEWING",
                "FIXING",
                "MERGING",
                "COMPLETED",
                "LANDED",
                "FAILED",
                "DEFERRED",
                "ABANDONED",
                "SUPERSEDED",
                "WAITING_REVIEW",
                "WAITING_FIX",
                "WAITING_MERGE",
                "WAITING_MERGE_AGENT",
                "STALE",
                "AWAITING_DECISION",
                "BLOCKED_BY_TASK",
                "BLOCKED_BY_CONDITION",
                "BLOCKED_BY_SYNC",
                "BLOCKED_BY_LEASE",
            ],
        ),
        (Action, ["implement", "review", "fix", "merge", "sync", "blocked"]),
        (
            Event,
            [
                "complete",
                "approve",
                "reject",
                "landed",
                "own_defect",
                "release",
                "release_blocked",
                "expired",
            ],
        ),
        (JobKind, ["land", "sync"]),
        (
            JobState,
            ["running", "needs_agent", "succeeded", "own_defect", "condition_unmet", "expired"],
        ),
        (ConditionStage, ["claim", "landing"]),
        (DecisionEffect, ["none", "abandon", "defer", "reopen", "drop_edge"]),
    ],
)
def test_each_vocabulary_holds_exactly_its_stored_values(
    enum: type[StrEnum], values: list[str]
) -> None:
    assert [member.value for member in enum] == values


@pytest.mark.parametrize("enum", [Status, DecisionStatus, Phase, DisplayStatus])
def test_upper_case_vocabularies_store_their_member_name(enum: type[StrEnum]) -> None:
    assert all(member.value == member.name for member in enum)


def test_every_stored_status_is_exactly_one_of_in_step_stable_or_exit() -> None:
    assert IN_STEP | STABLE | EXITS == frozenset(Status)
    assert not IN_STEP & STABLE
    assert not IN_STEP & EXITS
    assert not STABLE & EXITS


def test_in_step_statuses_are_the_ing_statuses() -> None:
    assert IN_STEP == {s for s in Status if s.value.endswith("ING")}


def test_a_rollup_sets_aside_exactly_the_exits() -> None:
    assert SET_ASIDE == {Status.DEFERRED, Status.ABANDONED, Status.SUPERSEDED}
