"""Which model family runs a step. tm names it at claim; the workflow maps a family to a model id."""

from taskmanager.core.enums import NodeKind
from taskmanager.core.models import Node
from taskmanager.core.status import Action, Outcome

# Cheapest first: `_families` sorts by this order, so [0] is the cheapest and [-1] the strongest.
FAMILIES = ("haiku", "sonnet", "opus", "fable")
STRONG = frozenset({"opus", "fable"})


def family(model_id: str) -> str | None:
    lowered = model_id.lower()
    return next((name for name in FAMILIES if name in lowered), None)


def _families(model_ids: list[str]) -> list[str]:
    found = {name for model_id in model_ids if (name := family(model_id)) is not None}
    return sorted(found, key=FAMILIES.index)


def _at_least_opus(families: list[str]) -> str:
    strongest = families[-1] if families else "opus"
    return strongest if strongest in STRONG else "opus"


def model_for(action: Action, node: Node, fix_round: int) -> str:
    families = _families(node.acceptable_models)
    review_families = _families([str(m) for m in node.frontmatter.get("review_models") or []])
    container = node.kind in (NodeKind.PLAN, NodeKind.SPEC)
    if action == Action.IMPLEMENT:
        return families[0] if families else "sonnet"
    if action == Action.REVIEW:
        if review_families:
            return review_families[0]
        # A container review is a branch review.
        return _at_least_opus(families) if container else "sonnet"
    if action == Action.FIX:
        if node.fix_for == Outcome.MERGE_FAILED:
            return "sonnet"
        # fix_round is the 1-based review round the fix answers; only a container reaches 3.
        if fix_round >= 3:
            return _at_least_opus(families)
        implementer = families[0] if families else "sonnet"
        return implementer if implementer in STRONG else "sonnet"
    if action in (Action.MERGE, Action.SYNC):
        return "sonnet"
    raise ValueError(f"no model runs a {action} step")
