import json
from pathlib import Path
from typing import Any

import pytest

from taskmanager.web.visibility import (
    Filters,
    facets,
    parse_filters,
    project_edges,
    visible_ids,
)

FIXTURE = json.loads(
    (Path(__file__).parent.parent / "fixtures" / "visibility_cases.json").read_text()
)


@pytest.mark.parametrize("case", FIXTURE["cases"], ids=[c["name"] for c in FIXTURE["cases"]])
def test_visibility_case_matches_its_golden_vector(case: dict[str, Any]) -> None:
    estate = case.get("estate", FIXTURE)
    rows = estate["rows"]
    filters = parse_filters(case["filters"])

    visible = visible_ids(rows, filters, case["open"])

    assert visible == case["visible"]
    assert project_edges(estate["edges"], rows, visible) == case["edges"]
    assert facets(rows, filters) == case["facets"]


def test_parse_filters_ignores_an_unknown_status_code() -> None:
    filters = parse_filters({"status": "READY,NOT_A_STATUS", "xstatus": "ALSO_NOT_A_STATUS"})

    assert dict(filters.status_mode) == {"READY": "include"}


def test_parse_filters_ignores_an_unknown_phase_code() -> None:
    filters = parse_filters({"phase": "QUEUED,NOT_A_PHASE"})

    assert dict(filters.phase_mode) == {"QUEUED": "include"}


def test_parse_filters_an_exclude_wins_over_an_include_for_the_same_value() -> None:
    filters = parse_filters({"repo": "repoA", "xrepo": "repoA"})

    assert dict(filters.repo_mode) == {"repoA": "exclude"}


def test_parse_filters_a_missing_or_empty_key_means_no_opinion() -> None:
    filters = parse_filters({})

    assert filters == Filters()


def test_parse_filters_lowercases_q() -> None:
    assert parse_filters({"q": "GiZmO"}).q == "gizmo"


def test_parse_filters_rejects_a_non_numeric_smin() -> None:
    with pytest.raises(ValueError, match="smin/smax must be a number"):
        parse_filters({"smin": "not-a-number"})


def test_parse_filters_rejects_a_non_numeric_smax() -> None:
    with pytest.raises(ValueError, match="smin/smax must be a number"):
        parse_filters({"smax": "not-a-number"})


def test_parse_filters_accepts_decimal_score_bounds() -> None:
    filters = parse_filters({"smin": "12.5", "smax": "99"})

    assert filters.score_min == 12.5
    assert filters.score_max == 99.0
