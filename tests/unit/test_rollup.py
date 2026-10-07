import pytest

from taskmanager.core.rollup import rollup
from taskmanager.core.status import Status

S = Status


@pytest.mark.parametrize(
    ("current", "children", "derived"),
    [
        (S.READY, [], S.READY),
        (S.IMPLEMENTED, [], S.READY),
        (S.READY, [S.READY], S.READY),
        (S.READY, [S.COMPLETED, S.IMPLEMENTING], S.READY),
        (S.READY, [S.COMPLETED], S.IMPLEMENTED),
        (S.READY, [S.COMPLETED, S.COMPLETED], S.IMPLEMENTED),
        (S.READY, [S.COMPLETED, S.DEFERRED, S.ABANDONED, S.SUPERSEDED], S.IMPLEMENTED),
        (S.IMPLEMENTED, [S.COMPLETED], S.IMPLEMENTED),
        (S.REVIEWED, [S.COMPLETED], S.REVIEWED),
        (S.FIXED, [S.COMPLETED], S.FIXED),
        (S.IMPLEMENTED, [S.COMPLETED, S.READY], S.READY),
        (S.REVIEWED, [S.COMPLETED, S.FAILED], S.READY),
        (S.FIXED, [S.COMPLETED, S.READY], S.READY),
        (S.READY, [S.SUPERSEDED], S.COMPLETED),
        (S.READY, [S.SUPERSEDED, S.SUPERSEDED], S.COMPLETED),
        (S.READY, [S.SUPERSEDED, S.DEFERRED], S.DEFERRED),
        (S.READY, [S.ABANDONED, S.DEFERRED], S.DEFERRED),
        (S.READY, [S.ABANDONED], S.ABANDONED),
        (S.READY, [S.ABANDONED, S.SUPERSEDED], S.ABANDONED),
        (S.IMPLEMENTED, [S.DEFERRED], S.DEFERRED),
    ],
)
def test_a_container_status_is_derived_from_its_counted_children(
    current: Status, children: list[Status], derived: Status
) -> None:
    assert rollup(current, children) == derived


@pytest.mark.parametrize(
    "current",
    [
        S.REVIEWING,
        S.FIXING,
        S.MERGING,
        S.IMPLEMENTING,
        S.LANDED,
        S.COMPLETED,
        S.FAILED,
        S.DEFERRED,
        S.ABANDONED,
        S.SUPERSEDED,
    ],
)
@pytest.mark.parametrize("children", [[], [S.READY], [S.COMPLETED], [S.DEFERRED]])
def test_a_status_the_container_holds_on_its_own_is_never_re_derived(
    current: Status, children: list[Status]
) -> None:
    assert rollup(current, children) == current
