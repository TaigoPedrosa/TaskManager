"""A node's claimed steps as the audit ledger recorded them: who ran each, when, how it ended, and
everything it recorded on the node meanwhile. The ledger is the one record of a review round once
it has ended: a finding keeps only its latest state and answer, and a reopen clears the answer an
earlier fix gave. Every entry inside a step is one of its items, so a record a step newly writes
shows here without a change to this module."""

from collections.abc import Sequence
from typing import Any

from taskmanager.core.models import LedgerEvent
from taskmanager.core.status import Event

# The name an item of these ledger commands reads as; any other command reads as itself.
ITEMS = {
    "finding add": "raised",
    "finding open": "reopened",
    "finding closed": "closed",
    "finding disputed": "disputed",
    "check add": "check",
    "note add": "note",
}

_VERDICTS = (Event.APPROVE.value, Event.REJECT.value)


def steps_of(events: Sequence[LedgerEvent]) -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = []
    rounds = 0
    for e in events:
        command, payload = str(e.command), e.payload
        if command == "task start":
            steps.append(
                {
                    "action": payload["action"],
                    "agent": payload["agent"],
                    "started": e.timestamp.isoformat(),
                    "ended": None,
                    "outcome": None,
                    "to": None,
                    "round": None,
                    "items": [],
                }
            )
            continue
        # A claim whose worktree or branch could not be cut never ran.
        if command == "task start undone":
            if steps and steps[-1]["ended"] is None:
                steps.pop()
            continue
        # What happens between steps (an edit, a verification run) is no step's record.
        if not steps or steps[-1]["ended"] is not None:
            continue
        step = steps[-1]
        # Every event that moves a node ends the step its lease was for.
        if "to" not in payload:
            step["items"].append({**payload, "kind": ITEMS.get(command, command)})
        else:
            step["ended"] = e.timestamp.isoformat()
            step["outcome"] = payload.get("event")
            step["to"] = payload["to"]
            if step["outcome"] in _VERDICTS:
                rounds += 1
                step["round"] = rounds
    return steps
