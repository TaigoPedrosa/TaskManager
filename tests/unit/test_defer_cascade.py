import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from lifecycle_estate import add, make_estate, stored

from taskmanager.core.enums import NodeKind
from taskmanager.core.status import Action, Status
from taskmanager.engine.claims import Claims
from taskmanager.engine.discovery import discover


def batch(claims: Claims) -> dict[str, Any]:
    payload, _ = discover(claims, specs=None, session="s1", slots=10, max_strong=10)
    data: dict[str, Any] = json.loads(payload)
    return data


def chosen_ids(claims: Claims) -> list[str]:
    return [entry["id"] for entry in batch(claims)["chosen"]]


def tree(tmp_path: Path) -> Claims:
    claims = make_estate(tmp_path)
    add(claims, "S", NodeKind.SPEC)
    add(claims, "P", NodeKind.PLAN, parent="S")
    add(claims, "T", parent="P")
    assert chosen_ids(claims) == ["T"]
    return claims


def supersede(claims: Claims, node_id: str) -> None:
    add(claims, "S2", NodeKind.SPEC, status=Status.COMPLETED)
    claims.ops.supersede(node_id, "S2")


SET_ASIDE: dict[str, Callable[[Claims, str], object]] = {
    "DEFERRED": lambda claims, node_id: claims.defer(node_id, "later"),
    "ABANDONED": lambda claims, node_id: claims.abandon(node_id, "dropped"),
    "SUPERSEDED": supersede,
}


def test_a_task_under_a_deferred_spec_is_not_discovered(tmp_path: Path) -> None:
    claims = tree(tmp_path)

    claims.defer("S", "later")

    data = batch(claims)
    assert data["chosen"] == []
    assert "T: under S (DEFERRED)" in data["held"]
    assert stored(claims, "T").status == Status.READY


def test_start_under_a_deferred_spec_is_refused_naming_the_spec(tmp_path: Path) -> None:
    claims = tree(tmp_path)
    claims.defer("S", "later")

    result = claims.start("T", "agent", "s1")

    assert (result.action, result.reason) == (Action.BLOCKED, "under S (DEFERRED)")


def test_reopening_the_spec_makes_the_task_discoverable_again(tmp_path: Path) -> None:
    claims = tree(tmp_path)
    claims.defer("S", "later")

    claims.reopen("S", "back")

    assert chosen_ids(claims) == ["T"]


def test_a_deferred_plan_holds_its_task_naming_the_plan(tmp_path: Path) -> None:
    claims = tree(tmp_path)

    claims.defer("P", "later")

    assert "T: under P (DEFERRED)" in batch(claims)["held"]


@pytest.mark.parametrize("status", sorted(SET_ASIDE))
def test_a_task_under_a_set_aside_spec_is_held_and_refused(tmp_path: Path, status: str) -> None:
    claims = tree(tmp_path)

    SET_ASIDE[status](claims, "S")

    data = batch(claims)
    assert data["chosen"] == []
    assert f"T: under S ({status})" in data["held"]
    assert claims.start("T", "agent", "s1").reason == f"under S ({status})"
