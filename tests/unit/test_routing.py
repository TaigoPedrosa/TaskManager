import json
from pathlib import Path

import pytest
from lifecycle_estate import add, gated, make_estate

from taskmanager.core.enums import NodeKind
from taskmanager.core.models import Node
from taskmanager.core.status import Action, Outcome
from taskmanager.engine.config import ConfigStore, ModelsConfig
from taskmanager.engine.discovery import discover
from taskmanager.engine.routing import model_for
from taskmanager.engine.simulate import simulate

MODELS = ModelsConfig(default="cfg-default", merge="cfg-merge", strong=["big-1", "big-2"])


def node(
    kind: NodeKind = NodeKind.TASK,
    models: list[str] | None = None,
    review_models: list[str] | None = None,
    fix_for: Outcome | None = None,
) -> Node:
    return Node(
        id="N1",
        kind=kind,
        title="N1",
        acceptable_models=models or [],
        frontmatter={"review_models": review_models} if review_models else {},
        fix_for=fix_for,
    )


def test_a_claim_names_the_listed_model_id_verbatim() -> None:
    assert model_for(Action.IMPLEMENT, node(models=["gemini-3-pro", "big-1"]), 0, MODELS) == (
        "gemini-3-pro"
    )


@pytest.mark.parametrize(
    ("action", "kind", "models", "review_models", "fix_for", "fix_round", "expected"),
    [
        (Action.IMPLEMENT, NodeKind.TASK, [], [], None, 0, "cfg-default"),
        (Action.REVIEW, NodeKind.TASK, ["small", "big-1"], ["rev-a", "rev-b"], None, 0, "rev-a"),
        (Action.REVIEW, NodeKind.TASK, ["small", "big-1"], [], None, 0, "small"),
        (Action.REVIEW, NodeKind.PLAN, ["small", "gpt-9"], [], None, 0, "gpt-9"),
        (Action.REVIEW, NodeKind.SPEC, [], [], None, 0, "cfg-default"),
        (Action.FIX, NodeKind.TASK, ["big-1", "big-2"], [], Outcome.REJECT, 1, "big-1"),
        (Action.FIX, NodeKind.TASK, ["small", "mid"], [], Outcome.REJECT, 1, "mid"),
        (Action.FIX, NodeKind.PLAN, ["big-1", "top"], [], Outcome.REJECT, 2, "big-1"),
        (Action.FIX, NodeKind.PLAN, ["big-1", "top"], [], Outcome.REJECT, 3, "top"),
        (Action.FIX, NodeKind.TASK, [], [], Outcome.REJECT, 1, "cfg-default"),
        (Action.FIX, NodeKind.TASK, ["big-1"], [], Outcome.MERGE_FAILED, 0, "cfg-merge"),
        (Action.MERGE, NodeKind.TASK, ["big-1"], [], None, 0, "cfg-merge"),
        (Action.SYNC, NodeKind.PLAN, ["big-1"], [], None, 0, "cfg-merge"),
    ],
)
def test_each_step_is_routed_to_a_model_id_from_the_node_or_config(
    action: Action,
    kind: NodeKind,
    models: list[str],
    review_models: list[str],
    fix_for: Outcome | None,
    fix_round: int,
    expected: str,
) -> None:
    built = node(kind, models, review_models, fix_for)
    assert model_for(action, built, fix_round, MODELS) == expected


def test_a_blocked_answer_has_no_model() -> None:
    with pytest.raises(ValueError, match="no model runs"):
        model_for(Action.BLOCKED, node(), 0, MODELS)


def test_the_model_keys_default_to_the_current_claude_ids() -> None:
    assert ModelsConfig().model_dump() == {
        "default": "claude-sonnet-5-5",
        "merge": "claude-sonnet-5-5",
        "strong": ["claude-opus-5-5", "claude-fable-5-1"],
    }


def test_the_model_keys_are_set_through_config(tmp_path: Path) -> None:
    store = ConfigStore(tmp_path)
    store.set("models.default", "gemini-3-flash")
    store.set("models.strong", "[gemini-3-pro, gpt-9]")
    project = store.project()
    assert (project.models.default, project.models.merge, project.models.strong) == (
        "gemini-3-flash",
        "claude-sonnet-5-5",
        ["gemini-3-pro", "gpt-9"],
    )


def test_an_id_listed_in_models_strong_counts_against_max_strong_and_another_does_not(
    tmp_path: Path,
) -> None:
    config = gated("api").model_copy(update={"models": ModelsConfig(strong=["gemini-3-pro"])})
    claims = make_estate(tmp_path, config=config)
    add(claims, "T1", models=["gemini-3-pro"], priority=90)
    add(claims, "T2", models=["claude-opus-5-5"], priority=80)

    payload, _ = discover(claims, specs=None, session="s1", slots=10, max_strong=0)
    data = json.loads(payload)

    assert [(e["id"], e["model"]) for e in data["chosen"]] == [("T2", "claude-opus-5-5")]
    assert "T1: no free strong-model slot" in data["held"]


def test_a_claim_and_a_simulated_wave_name_the_configured_default(tmp_path: Path) -> None:
    models = ModelsConfig(default="gemini-3-flash")
    claims = make_estate(tmp_path, config=gated("api").model_copy(update={"models": models}))
    add(claims, "T1")
    add(claims, "T2")

    assert claims.start("T1", "agent-1", "s1").model == "gemini-3-flash"
    waves = simulate(claims.snapshots.build(), 1, 10, 10, None, models=models)

    assert {entry.id: entry.model for entry in waves[0].entries} == {
        "T1": "gemini-3-flash",
        "T2": "gemini-3-flash",
    }
