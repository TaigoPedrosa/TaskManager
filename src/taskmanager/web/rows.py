"""Rows, the counts tree and its hash: one `DisplayView` turned into what the live view sends."""

import hashlib
import json
from collections import Counter
from collections.abc import Mapping
from typing import Any, Final, Literal

from taskmanager.core.enums import NodeKind
from taskmanager.core.status import SET_ASIDE, DecisionStatus, Status
from taskmanager.engine.heuristics import score_every_task
from taskmanager.engine.snapshot import DisplayView, phase_of, stored_status, waits_on
from taskmanager.engine.stepgraph import Snapshot

type ArchivedMode = Literal["exclude", "include", "only"]
ARCHIVED_MODES: Final[tuple[ArchivedMode, ...]] = ("exclude", "include", "only")


def _lease_row(snapshot: Snapshot, node_id: str) -> dict[str, Any] | None:
    lease = snapshot.graph_data().leases.get(node_id)
    if lease is None:
        return None
    return {"agent_id": lease.agent_id, "action": lease.action.value if lease.action else None}


def build_rows(
    view: DisplayView, archived: frozenset[str] = frozenset()
) -> dict[str, dict[str, Any]]:
    snapshot = view.snapshot
    data = snapshot.graph_data()
    scores = score_every_task(snapshot)
    rows: dict[str, dict[str, Any]] = {}
    for node_id, node in data.nodes.items():
        if node.kind == NodeKind.DECISION:
            continue
        work, decisions = waits_on(snapshot, node)
        rows[node_id] = {
            "id": node.id,
            "kind": node.kind.value,
            "title": node.title,
            "ordinal": node.ordinal,
            "priority": node.priority,
            "parent": snapshot.parent(node_id),
            "status": stored_status(node).value,
            "display": view.display(node),
            "phase": phase_of(node),
            "score": scores.get(node_id) if node.kind == NodeKind.TASK else None,
            "target_repo": node.target_repo,
            "acceptable_models": node.acceptable_models,
            "review": node.review,
            "fix": node.fix,
            "merge": node.merge.value,
            "outcome": node.outcome.value if node.outcome else None,
            "requires": node.requires,
            "lease": _lease_row(snapshot, node_id),
            "waits_on": [*work, *decisions],
            "superseded_by": view.superseded_by(node_id),
            "child_count": len(snapshot.children(node_id)),
            "rev": data.revs.get(node_id, 0),
            "archived": node_id in archived,
        }
    return rows


def rows_in(rows: dict[str, dict[str, Any]], mode: ArchivedMode) -> dict[str, dict[str, Any]]:
    # An archive holds whole spec subtrees, so either side keeps every row's parent.
    if mode == "include":
        return rows
    only = mode == "only"
    return {node_id: row for node_id, row in rows.items() if row["archived"] is only}


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def row_digest(row: dict[str, Any]) -> str:
    return hashlib.sha256(canonical(row).encode("utf-8")).hexdigest()


def statuses_hash(entries: list[dict[str, Any]]) -> str:
    return hashlib.sha256(canonical(entries).encode("utf-8")).hexdigest()


def _sort_key(value: str | None) -> tuple[int, str]:
    return (0, "") if value is None else (1, value)


_NOT_A_CYCLE = frozenset({Status.READY.value, *(status.value for status in SET_ASIDE)})


def counts_as_work(row: Mapping[str, Any]) -> bool:
    # Once a container's own cycle has started (reviewed, fixed, merged, or done), that cycle is
    # a unit of work too. READY covers both "not started" and the in-progress roll-up (displayed
    # IMPLEMENTING), so it never counts.
    if row["kind"] == NodeKind.TASK.value:
        return True
    return bool(row["child_count"]) and row["status"] not in _NOT_A_CYCLE


def _nearest_ancestor(rows: dict[str, dict[str, Any]], node_id: str, kind: str) -> str | None:
    current: str | None = rows[node_id]["parent"]
    while current is not None:
        row = rows.get(current)
        if row is None:
            return None
        if row["kind"] == kind:
            return current
        current = row["parent"]
    return None


def _counted_display(row: Mapping[str, Any]) -> str:
    """A superseded node is done once its replacement is, and set aside until then."""
    by = row.get("superseded_by")
    return (
        Status.COMPLETED.value if by and by["status"] == Status.COMPLETED.value else row["display"]
    )


def statuses(rows: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[str | None, dict[str | None, Counter[str]]] = {}
    for row in rows.values():
        if row["kind"] == NodeKind.SPEC.value:
            groups.setdefault(row["id"], {})
        elif row["kind"] == NodeKind.PLAN.value:
            spec_id = _nearest_ancestor(rows, row["id"], NodeKind.SPEC.value)
            groups.setdefault(spec_id, {}).setdefault(row["id"], Counter())
        elif row["kind"] == NodeKind.TASK.value:
            spec_id = _nearest_ancestor(rows, row["id"], NodeKind.SPEC.value)
            plan_id = _nearest_ancestor(rows, row["id"], NodeKind.PLAN.value)
            counts = groups.setdefault(spec_id, {}).setdefault(plan_id, Counter())
            counts[_counted_display(row)] += 1

    # A container's own step counts in the same (spec, plan) group its children roll up into,
    # under its own display.
    for row in rows.values():
        if row["kind"] == NodeKind.TASK.value or not counts_as_work(row):
            continue
        if row["kind"] == NodeKind.SPEC.value:
            groups[row["id"]].setdefault(None, Counter())[row["display"]] += 1
        else:
            spec_id = _nearest_ancestor(rows, row["id"], NodeKind.SPEC.value)
            groups[spec_id][row["id"]][row["display"]] += 1

    return [
        {
            "spec": spec_id,
            "plans": [
                {"plan": plan_id, "counts": dict(groups[spec_id][plan_id])}
                for plan_id in sorted(groups[spec_id], key=_sort_key)
            ],
        }
        for spec_id in sorted(groups, key=_sort_key)
    ]


def decisions_open(view: DisplayView) -> int:
    return sum(
        1
        for node in view.snapshot.graph_data().nodes.values()
        if node.kind == NodeKind.DECISION and node.status == DecisionStatus.OPEN
    )
