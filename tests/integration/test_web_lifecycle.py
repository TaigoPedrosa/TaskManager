"""The web API on the lifecycle model: stored status beside the derived display and phase,
flags and conditions, verbs instead of a status setter, and job state."""

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from taskmanager.core.models import Lease
from taskmanager.core.status import Action, DecisionStatus, DisplayStatus, Phase, Status
from taskmanager.db.cache_repo import CacheRepository
from taskmanager.db.connection import DatabaseManager
from taskmanager.db.node_repo import NodeRepository
from taskmanager.db.runtime_repo import RuntimeRepository
from taskmanager.di.container import create_container
from taskmanager.engine.operations import Operations
from taskmanager.web.app import create_app

JSON = {"content-type": "application/json"}
Web = tuple[TestClient, Path]


@pytest.fixture
def web(tmp_path: Path) -> Web:
    DatabaseManager(tmp_path / ".taskmanager").init_all()
    ops = create_container(tmp_path).get(Operations)
    spec = ops.add_spec("S", slug="S1")
    plan = ops.add_plan("P", spec, slug="P1", review=True, fix=True)
    a = ops.add_task("a", plan, slug="a")
    ops.add_task("b", plan, slug="b", depends_on=[a])
    return TestClient(create_app(tmp_path)), tmp_path


def repo(root: Path) -> NodeRepository:
    return NodeRepository(DatabaseManager(root / ".taskmanager"))


def plan_children(client: TestClient) -> dict[str, dict[str, Any]]:
    spec = next(n for n in client.get("/api/tree").json() if n["id"] == "S1")
    return {t["id"]: t for t in spec["children"][0]["children"]}


def test_tree_nodes_carry_the_stored_status_display_phase_and_flags(web: Web) -> None:
    client, _root = web
    tasks = plan_children(client)
    a, b = tasks["S1-P1-a"], tasks["S1-P1-b"]
    assert (a["status"], a["display"], a["phase"]) == ("READY", "READY", "QUEUED")
    assert (b["status"], b["display"], b["phase"]) == ("READY", "BLOCKED_BY_TASK", "QUEUED")
    assert (a["review"], a["fix"], a["merge"], a["branch"]) == (True, True, "main", "tm/S1-P1-a")
    assert "virtual_status" not in a
    spec = next(n for n in client.get("/api/tree").json() if n["id"] == "S1")
    plan = spec["children"][0]
    assert (plan["review"], plan["fix"], plan["display"]) == (True, True, "READY")


def test_node_detail_carries_chains_dependencies_conditions_and_jobs(web: Web) -> None:
    client, _root = web
    detail = client.get("/api/nodes/S1-P1-b").json()
    assert (detail["display"], detail["phase"]) == ("BLOCKED_BY_TASK", "QUEUED")
    assert detail["node"]["landing_chain"] == ["S1-P1-b"]
    assert detail["node"]["base_chain"] == ["MAIN"]
    assert [(d["id"], d["status"], d["finished"]) for d in detail["dependency_details"]] == [
        ("S1-P1-a", "READY", False)
    ]
    assert (detail["conditions"], detail["jobs"]) == ([], [])


def test_detail_lists_conditions_with_their_stage_and_last_result(web: Web) -> None:
    client, root = web
    res = client.post(
        "/api/nodes/S1-P1-a/conditions",
        json={"needs": "staging up", "command": "true", "stage": "landing"},
    )
    assert res.status_code == 201
    idx = res.json()["idx"]
    detail = client.get("/api/nodes/S1-P1-a").json()
    assert [(c["needs"], c["stage"], c["last_result"]) for c in detail["conditions"]] == [
        ("staging up", "landing", None)
    ]
    create_container(root).get(CacheRepository).put_condition("S1-P1-a", idx, "true", 0)
    detail = client.get("/api/nodes/S1-P1-a").json()
    assert detail["conditions"][0]["last_result"] == 0
    assert client.delete(f"/api/nodes/S1-P1-a/conditions/{idx}", headers=JSON).status_code == 200
    assert client.get("/api/nodes/S1-P1-a").json()["conditions"] == []


def test_a_prose_condition_is_refused(web: Web) -> None:
    client, _root = web
    res = client.post(
        "/api/nodes/S1-P1-a/conditions",
        json={"needs": "sign-off", "command": "the design is signed off"},
    )
    assert res.status_code == 400 and "decision" in res.json()["detail"]


def test_stats_count_every_display_status_and_phase_including_zeros(web: Web) -> None:
    client, _root = web
    stats = client.get("/api/stats").json()
    assert stats["total"] == 2
    assert set(stats["display"]) == {d.value for d in DisplayStatus}
    assert set(stats["phase"]) == {p.value for p in Phase}
    assert {k: v for k, v in stats["display"].items() if v} == {"READY": 1, "BLOCKED_BY_TASK": 1}
    assert {k: v for k, v in stats["phase"].items() if v} == {"QUEUED": 2}


def test_meta_lists_the_lifecycle_vocabularies(web: Web) -> None:
    client, _root = web
    meta = client.get("/api/meta").json()
    assert meta["statuses"] == [s.value for s in Status]
    assert meta["display_statuses"] == [d.value for d in DisplayStatus]
    assert meta["phases"] == [p.value for p in Phase]
    assert meta["decision_statuses"] == ["OPEN", "ANSWERED", "WITHDRAWN"]
    assert meta["reset_targets"] == ["READY", "IMPLEMENTED", "REVIEWED", "FIXED", "COMPLETED"]
    assert meta["merge_targets"] == ["parent", "main"]
    assert "none" in meta["decision_effects"]


def test_there_is_no_route_that_sets_a_status(web: Web) -> None:
    client, _root = web
    res = client.post("/api/nodes/S1-P1-a/status", json={"status": "COMPLETED"})
    assert res.status_code in (404, 405)
    assert repo(web[1]).get_node("S1-P1-a").status == Status.READY  # type: ignore[union-attr]


@pytest.mark.parametrize(
    ("verb", "body", "status", "section"),
    [
        ("defer", {"note": "after launch"}, Status.DEFERRED, "deferral"),
        ("abandon", {"note": "dropped"}, Status.ABANDONED, "abandonment"),
        ("reset", {"note": "repair", "to": "IMPLEMENTED"}, Status.IMPLEMENTED, None),
    ],
)
def test_a_verb_moves_the_node_and_records_its_note(
    web: Web, verb: str, body: dict[str, str], status: Status, section: str | None
) -> None:
    client, root = web
    res = client.post(f"/api/nodes/S1-P1-a/{verb}", json=body)
    assert res.status_code == 200, res.text
    assert res.json()["status"] == status.value
    node_repo = repo(root)
    assert node_repo.get_node("S1-P1-a").status == status  # type: ignore[union-attr]
    if section is not None:
        assert node_repo.get_section("S1-P1-a", section) is not None


def test_reopen_returns_a_deferred_node_to_ready(web: Web) -> None:
    client, _root = web
    client.post("/api/nodes/S1-P1-a/defer", json={"note": "later"})
    res = client.post("/api/nodes/S1-P1-a/reopen", json={"note": "now", "new_branch": False})
    assert res.status_code == 200 and res.json()["status"] == "READY"


def test_a_verb_needs_a_note_and_reset_refuses_a_step_status(web: Web) -> None:
    client, root = web
    assert client.post("/api/nodes/S1-P1-a/defer", json={}).status_code == 422
    refused = client.post("/api/nodes/S1-P1-a/reset", json={"note": "x", "to": "REVIEWING"})
    assert refused.status_code in (400, 409)
    assert repo(root).get_node("S1-P1-a").status == Status.READY  # type: ignore[union-attr]


def test_patch_sets_flags_merge_requires_and_land_order_through_validation(web: Web) -> None:
    client, root = web
    # b depends on a; landing a on the parent plan while b still lands on MAIN would make b's
    # start wait on the plan's landing while the plan's landing waits on b -- a real deadlock
    # the step-graph check refuses (spec S4.4). Moving b onto the same parent target first keeps
    # both landing chains meeting at `a` instead of routing through the plan.
    assert client.patch("/api/nodes/S1-P1-b", json={"merge": "parent"}).status_code == 200
    ok = client.patch(
        "/api/nodes/S1-P1-a", json={"merge": "parent", "fix": False, "requires": ["figma"]}
    )
    assert ok.status_code == 200, ok.text
    assert client.patch("/api/nodes/S1-P1", json={"land_order": ["api", "web"]}).status_code == 200
    refused = client.patch("/api/nodes/S1-P1-b", json={"review": False})
    assert refused.status_code == 400
    node_repo = repo(root)
    a = node_repo.get_node("S1-P1-a")
    assert a is not None and (a.merge.value, a.fix, a.requires) == ("parent", False, ["figma"])
    assert node_repo.get_node("S1-P1").land_order == ["api", "web"]  # type: ignore[union-attr]
    assert node_repo.get_node("S1-P1-b").review is True  # type: ignore[union-attr]


def test_create_routes_take_the_flags(web: Web) -> None:
    client, root = web
    plan = client.post(
        "/api/plans", json={"title": "Q", "spec": "S1", "slug": "P2", "review": True, "fix": True}
    )
    assert plan.status_code == 201 and plan.json() == {"id": "S1-P2"}
    task = client.post(
        "/api/tasks",
        json={"title": "c", "plan": "S1-P2", "slug": "c", "merge": "parent", "requires": ["x"]},
    )
    assert task.status_code == 201
    node_repo = repo(root)
    p, c = node_repo.get_node("S1-P2"), node_repo.get_node("S1-P2-c")
    assert p is not None and (p.review, p.fix) == (True, True)
    assert c is not None and (c.merge.value, c.requires) == ("parent", ["x"])


def test_deleting_a_lease_gives_the_step_back_as_a_transient_release(web: Web) -> None:
    client, root = web
    node_repo = repo(root)
    node = node_repo.get_node("S1-P1-a")
    assert node is not None
    node.status, node.claimed_from = Status.IMPLEMENTING, Status.READY
    lease = Lease(
        task_id="S1-P1-a",
        agent_id="a",
        session_id="s",
        branch_name="tm/S1-P1-a",
        action=Action.IMPLEMENT,
        ttl_seconds=3600,
    )
    assert RuntimeRepository(node_repo.db).claim(lease, [], node)
    assert client.get("/api/nodes/S1-P1-a").json()["lease"]["action"] == "implement"
    assert client.delete("/api/nodes/S1-P1-a/lease", headers=JSON).status_code == 200
    after = node_repo.get_node("S1-P1-a")
    assert after is not None and (after.status, after.step_failures) == (Status.READY, 1)


def test_an_unknown_job_is_404(web: Web) -> None:
    client, _root = web
    assert client.get("/api/jobs/nope").status_code == 404


def test_decision_rows_use_decision_statuses_and_list_the_nodes_they_block(web: Web) -> None:
    client, _root = web
    res = client.post(
        "/api/decisions",
        json={
            "question": "Which way?",
            "slug": "way",
            "options": [{"key": "drop", "label": "Drop it", "effect": "abandon"}],
            "blocks": ["S1-P1-a"],
        },
    )
    assert res.status_code == 201
    rows = client.get("/api/decisions", params={"status": "open"}).json()
    assert [r["id"] for r in rows] == ["decision-way"]
    assert rows[0]["status"] == DecisionStatus.OPEN.value
    assert [(b["id"], b["display"]) for b in rows[0]["blocks"]] == [
        ("S1-P1-a", "AWAITING_DECISION")
    ]
    assert rows[0]["decision"]["options"][0]["effect"] == "abandon"
