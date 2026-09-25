from datetime import datetime
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field

from taskmanager.core.enums import NodeKind, RelationType
from taskmanager.core.lifecycle import LifecycleError, abandon, defer, reopen
from taskmanager.core.models import Node
from taskmanager.core.status import EXITS, DecisionEffect, DecisionStatus, Status
from taskmanager.engine.snapshot import apply_cycle, cycle_of, roll_up_ancestors, stored_status

if TYPE_CHECKING:
    from taskmanager.engine.operations import Operations

# How a decision's status reads everywhere a human sees it: the CLI table, `tm decision get`,
# the JSON and YAML rows.
DECISION_STATUS_LABELS: dict[DecisionStatus, str] = {
    DecisionStatus.OPEN: "Open",
    DecisionStatus.ANSWERED: "Answered",
    DecisionStatus.WITHDRAWN: "Withdrawn",
}

# A node already where an effect would put it is left as it is, not refused.
_REACHES = {DecisionEffect.ABANDON: Status.ABANDONED, DecisionEffect.DEFER: Status.DEFERRED}

# The section each effect appends its note to, beside the ledger entry.
_NOTE_SECTION = {
    DecisionEffect.ABANDON: "abandonment",
    DecisionEffect.DEFER: "deferral",
    DecisionEffect.REOPEN: "reopen",
}


class DecisionOption(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    label: str
    description: str = ""
    recommended: bool = False
    effect: DecisionEffect = DecisionEffect.NONE


class DecisionAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    option: str | None = None
    text: str = ""
    rationale: str = ""
    answered_by: str
    answered_at: datetime


class DecisionData(BaseModel):
    model_config = ConfigDict(extra="forbid")

    options: list[DecisionOption] = Field(default_factory=list)
    allow_custom: bool = True
    raised_by: str | None = None
    answer: DecisionAnswer | None = None
    withdrawn_reason: str = ""
    # The node the decision is about; `drop_edge` removes each blocked node's edge to it.
    subject: str | None = None
    # What a custom answer does, since it names no option to carry an effect.
    custom_effect: DecisionEffect = DecisionEffect.NONE


def read_decision(node: Node) -> DecisionData:
    raw: Any = node.frontmatter.get("decision") or {}
    return DecisionData.model_validate(raw)


def write_decision(node: Node, data: DecisionData) -> None:
    node.frontmatter["decision"] = data.model_dump(mode="json")


def chosen_effect(data: DecisionData) -> DecisionEffect:
    if data.answer is None:
        return DecisionEffect.NONE
    if data.answer.option is None:
        return data.custom_effect
    return next(o.effect for o in data.options if o.key == data.answer.option)


def _note(decision_id: str, data: DecisionData) -> str:
    answer = data.answer
    if answer is None:
        return decision_id
    label = next((o.label for o in data.options if o.key == answer.option), "a custom answer")
    said = answer.text or answer.rationale
    return f"{decision_id} answered {label}" + (f": {said}" if said else "")


def stranded_dependents(ops: Operations, node_id: str) -> list[str]:
    """Nodes whose own edge points at `node_id` and that still have work ahead of them."""
    stranded = []
    for dependent_id in ops.node_repo.get_blocked_by(node_id):
        dependent = ops.node_repo.get_node(dependent_id)
        if dependent is None or dependent.kind == NodeKind.DECISION:
            continue
        status = stored_status(dependent)
        if status != Status.COMPLETED and status not in EXITS:
            stranded.append(dependent_id)
    return stranded


def open_failed_decision(ops: Operations, node_id: str, reason: str, evidence: str) -> str:
    return ops.add_decision(
        f"{node_id} failed: abandon, or investigate?",
        context=f"{reason}\n\n{evidence}".strip(),
        options=[
            DecisionOption(
                key="abandon",
                label="Abandon it",
                description="The work is dropped; anything depending on it gets its own ruling.",
                effect=DecisionEffect.ABANDON,
            ),
            DecisionOption(
                key="investigate",
                label="Investigate and reopen it",
                description="It goes back to the start of its cycle with this answer as its note.",
                effect=DecisionEffect.REOPEN,
            ),
        ],
        raised_by=node_id,
        blocks=[node_id],
        subject=node_id,
        custom_effect=DecisionEffect.REOPEN,
    )


def open_stranded_decision(
    ops: Operations, node_id: str, status: Status, dependents: list[str]
) -> str:
    return ops.add_decision(
        f"{node_id} was {status}: drop the edge, defer, or abandon the dependents?",
        options=[
            DecisionOption(
                key="drop_edge",
                label="Drop the edge",
                description=f"Each dependent stops waiting on {node_id}.",
                effect=DecisionEffect.DROP_EDGE,
            ),
            DecisionOption(
                key="defer",
                label="Defer the dependents",
                effect=DecisionEffect.DEFER,
            ),
            DecisionOption(
                key="abandon",
                label="Abandon the dependents",
                effect=DecisionEffect.ABANDON,
            ),
        ],
        raised_by=node_id,
        blocks=dependents,
        subject=node_id,
    )


def _children_all_completed(ops: Operations, node_id: str) -> bool:
    counted = [
        s
        for c in ops.node_repo.get_children(node_id)
        if (child := ops.node_repo.get_node(c)) is not None
        and (s := stored_status(child)) not in EXITS
    ]
    return bool(counted) and all(s == Status.COMPLETED for s in counted)


def _open_decisions_on(ops: Operations, node_id: str) -> list[str]:
    return [
        d
        for d in ops.node_repo.get_dependencies(node_id)
        if (dep := ops.node_repo.get_node(d)) is not None
        and dep.kind == NodeKind.DECISION
        and stored_status(dep) == DecisionStatus.OPEN
    ]


def apply_effect(ops: Operations, decision_id: str, effect: DecisionEffect) -> list[str]:
    """Apply an answered decision's effect to every node it blocks, inside the caller's
    transaction, and return the nodes it changed. A node the effect cannot apply to refuses the
    whole answer, so no node is ever half-ruled."""
    # Operations imports this module, so importing it back at load time would be circular.
    from taskmanager.engine.operations import OperationError

    if effect == DecisionEffect.NONE:
        return []
    decision = ops.node_repo.get_node(decision_id)
    if decision is None:
        raise OperationError(f"decision '{decision_id}' not found", 404)
    data = read_decision(decision)
    blocked = ops.node_repo.get_blocked_by(decision_id)
    busy = [n for n in blocked if ops.busy(n)]
    if busy:
        raise OperationError(
            f"'{decision_id}' cannot {effect} {', '.join(busy)} while a step runs: "
            "wait for it to end, or stop it",
            409,
        )
    note = _note(decision_id, data)
    for node_id in blocked:
        if effect == DecisionEffect.DROP_EDGE:
            if data.subject is None:
                raise OperationError(f"'{decision_id}' names no subject to drop an edge to", 400)
            ops.node_repo.remove_relation(node_id, data.subject, RelationType.DEPENDS_ON)
            continue
        node = ops.node_repo.get_node(node_id)
        if node is None or node.status == _REACHES.get(effect):
            continue
        if effect == DecisionEffect.REOPEN and (waiting := _open_decisions_on(ops, node_id)):
            raise OperationError(
                f"'{node_id}' cannot reopen while {', '.join(waiting)} is open", 409
            )
        try:
            if effect == DecisionEffect.ABANDON:
                cycle = abandon(cycle_of(node))
            elif effect == DecisionEffect.DEFER:
                cycle = defer(cycle_of(node))
            else:
                cycle = reopen(cycle_of(node), _children_all_completed(ops, node_id))
        except LifecycleError as exc:
            raise OperationError(
                f"'{decision_id}' cannot {effect} '{node_id}': {exc}", 409
            ) from exc
        updated = apply_cycle(node, cycle)
        if effect == DecisionEffect.REOPEN:
            updated.verdict = None
        ops.node_repo.save_node(updated)
        ops.append_section(node_id, _NOTE_SECTION[effect], note)
        if cycle.status in (Status.ABANDONED, Status.DEFERRED) and (
            dependents := stranded_dependents(ops, node_id)
        ):
            open_stranded_decision(ops, node_id, cycle.status, dependents)
        roll_up_ancestors(ops, node_id)
    return blocked
