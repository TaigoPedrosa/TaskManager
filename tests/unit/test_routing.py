import pytest

from taskmanager.core.enums import NodeKind
from taskmanager.core.models import Node
from taskmanager.core.status import Action, Outcome
from taskmanager.engine.routing import family, model_for


@pytest.mark.parametrize(
    ("model_id", "expected"),
    [
        ("claude-haiku-4-5", "haiku"),
        ("claude-sonnet-4-5", "sonnet"),
        ("claude-opus-5-5[1m]", "opus"),
        ("Fable-1", "fable"),
        ("gemini-2.5-pro", None),
    ],
)
def test_a_model_id_names_its_family(model_id: str, expected: str | None) -> None:
    assert family(model_id) == expected


@pytest.mark.parametrize(
    ("action", "kind", "models", "review_models", "fix_for", "fix_round", "expected"),
    [
        (
            Action.IMPLEMENT,
            NodeKind.TASK,
            ["claude-opus-4", "claude-sonnet-4"],
            [],
            None,
            0,
            "sonnet",
        ),
        (Action.IMPLEMENT, NodeKind.TASK, ["claude-haiku-4"], [], None, 0, "haiku"),
        (Action.IMPLEMENT, NodeKind.TASK, [], [], None, 0, "sonnet"),
        (Action.REVIEW, NodeKind.TASK, ["claude-opus-4"], [], None, 0, "sonnet"),
        (Action.REVIEW, NodeKind.TASK, ["claude-sonnet-4"], ["claude-opus-4"], None, 0, "opus"),
        (Action.REVIEW, NodeKind.PLAN, ["claude-sonnet-4"], [], None, 0, "opus"),
        (Action.REVIEW, NodeKind.SPEC, ["claude-sonnet-4", "fable-1"], [], None, 0, "fable"),
        (Action.REVIEW, NodeKind.PLAN, [], ["claude-haiku-4"], None, 0, "haiku"),
        (Action.FIX, NodeKind.TASK, ["claude-opus-4"], [], Outcome.REJECT, 1, "opus"),
        (
            Action.FIX,
            NodeKind.TASK,
            ["claude-haiku-4", "claude-opus-4"],
            [],
            Outcome.REJECT,
            2,
            "sonnet",
        ),
        (Action.FIX, NodeKind.TASK, ["fable-1"], [], Outcome.REJECT, 2, "fable"),
        (Action.FIX, NodeKind.PLAN, ["claude-sonnet-4"], [], Outcome.REJECT, 2, "sonnet"),
        (Action.FIX, NodeKind.PLAN, ["claude-sonnet-4"], [], Outcome.REJECT, 3, "opus"),
        (Action.FIX, NodeKind.PLAN, ["claude-sonnet-4", "fable-1"], [], Outcome.REJECT, 3, "fable"),
        (Action.FIX, NodeKind.TASK, ["claude-opus-4"], [], Outcome.MERGE_FAILED, 0, "sonnet"),
        (Action.MERGE, NodeKind.TASK, ["claude-opus-4"], [], None, 0, "sonnet"),
        (Action.SYNC, NodeKind.PLAN, ["claude-opus-4"], [], None, 0, "sonnet"),
    ],
)
def test_each_step_is_routed_to_its_model_family(
    action: Action,
    kind: NodeKind,
    models: list[str],
    review_models: list[str],
    fix_for: Outcome | None,
    fix_round: int,
    expected: str,
) -> None:
    node = Node(
        id="N1",
        kind=kind,
        title="N1",
        acceptable_models=models,
        frontmatter={"review_models": review_models} if review_models else {},
        fix_for=fix_for,
    )
    assert model_for(action, node, fix_round) == expected


def test_a_blocked_answer_has_no_model() -> None:
    with pytest.raises(ValueError, match="no model runs"):
        model_for(Action.BLOCKED, Node(id="N1", kind=NodeKind.TASK, title="N1"), 0)
