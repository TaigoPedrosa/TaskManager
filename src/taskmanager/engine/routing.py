"""Which model id runs a step. tm names it at claim, from the node's own model lists, which the
owner orders cheapest first; any string is a model id, of any vendor."""

from taskmanager.core.enums import CONTAINERS
from taskmanager.core.models import Node
from taskmanager.core.status import Action, Outcome
from taskmanager.engine.config import ModelsConfig


def model_for(action: Action, node: Node, fix_round: int, models: ModelsConfig) -> str:
    acceptable = node.acceptable_models
    reviewers = [str(m) for m in node.frontmatter.get("review_models") or []]
    cheapest = acceptable[0] if acceptable else models.default
    strongest = acceptable[-1] if acceptable else models.default
    if action == Action.IMPLEMENT:
        return cheapest
    if action == Action.REVIEW:
        if reviewers:
            return reviewers[0]
        # A container review is a branch review.
        return strongest if node.kind in CONTAINERS else cheapest
    if action == Action.FIX:
        if node.fix_for == Outcome.MERGE_FAILED:
            return models.merge
        # fix_round is the 1-based review round the fix answers; only a container reaches 3.
        if fix_round >= 3 or cheapest not in models.strong:
            return strongest
        return cheapest
    if action in (Action.MERGE, Action.SYNC):
        return models.merge
    raise ValueError(f"no model runs a {action} step")
