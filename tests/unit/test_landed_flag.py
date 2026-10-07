import pytest

from taskmanager.core.lifecycle import Cycle, LifecycleError, next_action, reset
from taskmanager.core.status import Action, Status

REVIEW_OFF = "this node has review off: nothing reviews it at LANDED"


@pytest.mark.parametrize("container", [False, True], ids=["task", "container"])
def test_next_action_at_landed_with_review_off_offers_no_review(container: bool) -> None:
    assert next_action(Cycle(Status.LANDED, container=container, review=False, fix=False)) is None


@pytest.mark.parametrize("container", [False, True], ids=["task", "container"])
def test_next_action_at_landed_with_review_on_offers_the_review(container: bool) -> None:
    assert next_action(Cycle(Status.LANDED, container=container)) == Action.REVIEW


@pytest.mark.parametrize("status", [Status.COMPLETED, Status.FAILED, Status.IMPLEMENTED])
def test_reset_to_landed_with_review_off_is_refused_with_its_message(status: Status) -> None:
    with pytest.raises(LifecycleError, match=REVIEW_OFF):
        reset(Cycle(status, container=True, review=False, fix=False), Status.LANDED, None)


def test_reset_to_landed_with_review_on_lands_the_node_with_its_review_next() -> None:
    landed = reset(Cycle(Status.COMPLETED, container=True), Status.LANDED, None)
    assert (landed.status, next_action(landed)) == (Status.LANDED, Action.REVIEW)


def test_reset_to_completed_with_review_off_is_accepted() -> None:
    completed = reset(
        Cycle(Status.FAILED, container=True, review=False, fix=False), Status.COMPLETED, None
    )
    assert completed.status == Status.COMPLETED
