from datetime import UTC, datetime, timedelta
from typing import Any

from taskmanager.core.models import LedgerEvent
from taskmanager.web.steps import steps_of

_T0 = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)


def _events(*entries: tuple[str, dict[str, Any]]) -> list[LedgerEvent]:
    return [
        LedgerEvent(
            id=i,
            timestamp=_T0 + timedelta(minutes=i),
            actor_id="cli",
            command=command,
            target_id="T1",
            payload=payload,
        )
        for i, (command, payload) in enumerate(entries, start=1)
    ]


def _start(action: str, agent: str) -> tuple[str, dict[str, Any]]:
    return ("task start", {"action": action, "agent": agent, "from": "READY"})


def _end(command: str, event: str, to: str) -> tuple[str, dict[str, Any]]:
    return (command, {"event": event, "from": "X", "to": to})


def test_a_claim_undone_leaves_no_step_and_the_next_claim_starts_clean() -> None:
    steps = steps_of(
        _events(
            _start("implement", "a"),
            ("task start undone", {}),
            _start("implement", "b"),
            _end("task complete", "complete", "IMPLEMENTED"),
        )
    )
    assert [(s["agent"], s["outcome"]) for s in steps] == [("b", "complete")]


def test_what_happens_between_steps_belongs_to_none() -> None:
    steps = steps_of(
        _events(
            ("task update", {"priority": 10}),
            _start("implement", "a"),
            ("verification_run", {"passed": True, "count": 2}),
            _end("task complete", "complete", "IMPLEMENTED"),
            ("verification_add", {"type": "test_command", "target": "t"}),
        )
    )
    assert steps[0]["items"] == [{"kind": "verification_run", "passed": True, "count": 2}]
    assert len(steps) == 1


def test_only_a_review_that_reached_a_verdict_counts_as_a_round() -> None:
    steps = steps_of(
        _events(
            _start("review", "r1"),
            _end("lease sweep", "expired", "IMPLEMENTED"),
            _start("review", "r2"),
            _end("task review", "reject", "REVIEWED"),
            _start("fix", "f1"),
            _end("task complete", "complete", "FIXED"),
            _start("review", "r3"),
            _end("task review", "approve", "REVIEWED"),
        )
    )
    assert [(s["agent"], s["round"]) for s in steps] == [
        ("r1", None),
        ("r2", 1),
        ("f1", None),
        ("r3", 2),
    ]
    assert steps[0]["ended"] == (_T0 + timedelta(minutes=2)).isoformat()


def test_a_step_still_running_has_no_end() -> None:
    steps = steps_of(_events(_start("review", "r1"), ("finding add", {"number": 1})))
    assert (steps[0]["ended"], steps[0]["outcome"], steps[0]["to"]) == (None, None, None)
    assert steps[0]["items"] == [{"kind": "raised", "number": 1}]
