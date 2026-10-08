"""An excluded node is the one a concurrent run of the same tick is about to claim, so its declared
files are as taken as a chosen node's."""

from pathlib import Path

import pytest
from test_selection import Estate, chosen, wave

from taskmanager.core.status import Status

OVERLAP = "declared_files overlap a node chosen this wave"


@pytest.fixture
def estate(tmp_path: Path) -> Estate:
    estate = Estate(tmp_path)
    estate.add("A", frontmatter={"declared_files": ["api/shared.py"]}, priority=90)
    estate.add("B", frontmatter={"declared_files": ["api/shared.py"]}, priority=80)
    return estate


def test_select_with_the_first_excluded_holds_the_second_for_the_shared_file(
    estate: Estate,
) -> None:
    w = wave(estate, exclude=("A",))
    assert chosen(w) == []
    assert w.held == ["A: excluded by args", f"B: {OVERLAP}"]


def test_select_with_nothing_excluded_chooses_the_first_and_holds_the_second(
    estate: Estate,
) -> None:
    w = wave(estate)
    assert chosen(w) == [("A", "implement")]
    assert w.held == [f"B: {OVERLAP}"]


def test_select_with_the_excluded_node_ordered_last_still_holds_the_other(
    estate: Estate,
) -> None:
    w = wave(estate, exclude=("B",))
    assert chosen(w) == []
    assert w.held == [f"A: {OVERLAP}", "B: excluded by args"]


def test_select_with_an_excluded_review_reserves_no_files(tmp_path: Path) -> None:
    estate = Estate(tmp_path)
    estate.add("A", status=Status.IMPLEMENTED, frontmatter={"declared_files": ["api/shared.py"]})
    estate.add("B", frontmatter={"declared_files": ["api/shared.py"]})
    w = wave(estate, exclude=("A",))
    assert chosen(w) == [("B", "implement")]
    assert w.held == ["A: excluded by args"]
