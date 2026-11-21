"""Which model runs a step. tm routes by ref, a tier or a pinned model id, from the node's own
lists, which the owner orders cheapest first; `resolve` turns a ref into what a harness runs."""

from typing import Final

from taskmanager.core.enums import CONTAINERS
from taskmanager.core.models import Node
from taskmanager.core.status import Action, Outcome
from taskmanager.engine import config
from taskmanager.engine.config import HarnessConfig, ModelsConfig, ProjectConfig

TIERS: Final = config.TIERS


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


# What each harness runs a tier on when `harnesses.<name>.models.<tier>` is not set.
BUILTIN_PROFILES: Final = {
    "claude-code": HarnessConfig(models={"light": "haiku", "standard": "sonnet", "strong": "opus"}),
}


def resolve(ref: str, harness: str, config: ProjectConfig) -> str | None:
    """The model `harness` runs `ref` on: a pinned id as it is, a tier as the harness's profile
    maps it, None when the profile leaves the tier to the harness's own default."""
    if ref not in TIERS:
        return ref
    profile: dict[str, str] = {}
    for source in (BUILTIN_PROFILES.get(harness), config.harnesses.get(harness)):
        profile.update(source.models if source else {})
    return profile.get(ref)
