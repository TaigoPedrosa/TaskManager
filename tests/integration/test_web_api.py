"""Integration tests for the write API (§5): generic routes only."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from taskmanager.core.enums import NodeKind, NodeStatus, RelationType, VerificationType
from taskmanager.core.models import Node, NodeRelation, NodeVerification
from taskmanager.db.connection import DatabaseManager
from taskmanager.db.ledger_repo import LedgerRepository
from taskmanager.db.node_repo import NodeRepository
from taskmanager.web.app import create_app

SAME_ORIGIN = "http://testserver"
# httpx sets Content-Type itself only for a `json=` body; a DELETE or a bodyless POST needs it
# spelled out, exactly as a real page's fetch call does under the write guard.
JSON = {"content-type": "application/json"}


@pytest.fixture
def api(tmp_path: Path) -> tuple[TestClient, NodeRepository, LedgerRepository]:
    db_mgr = DatabaseManager(tmp_path / ".taskmanager")
    db_mgr.init_all()
    node_repo = NodeRepository(db_mgr)
    ledger_repo = LedgerRepository(db_mgr)
    node_repo.save_node(Node(id="SPEC", kind=NodeKind.SPEC, title="Spec"))
    node_repo.save_node(Node(id="SPEC-P1", kind=NodeKind.PLAN, title="Plan"))
    node_repo.add_relation(
        NodeRelation(source_id="SPEC", target_id="SPEC-P1", relation_type=RelationType.CONTAINS)
    )
    node_repo.save_node(Node(id="SPEC-P1-T1", kind=NodeKind.TASK, title="Task", priority=60))
    node_repo.add_relation(
        NodeRelation(
            source_id="SPEC-P1", target_id="SPEC-P1-T1", relation_type=RelationType.CONTAINS
        )
    )
    node_repo.save_node(Node(id="SPEC-P1-T2", kind=NodeKind.TASK, title="Second"))
    node_repo.add_relation(
        NodeRelation(
            source_id="SPEC-P1", target_id="SPEC-P1-T2", relation_type=RelationType.CONTAINS
        )
    )
    app = create_app(tmp_path)
    client = TestClient(app)
    return client, node_repo, ledger_repo


def _last_actor(ledger_repo: LedgerRepository) -> str:
    events = ledger_repo.list_events(limit=1)
    assert events
    return events[0].actor_id


# -- write guard ----------------------------------------------------------------------------


def test_write_guard_refuses_missing_json_content_type(
    api: tuple[TestClient, NodeRepository, LedgerRepository],
) -> None:
    client, node_repo, _ledger_repo = api
    res = client.post(
        "/api/specs",
        content=b'{"title": "New Spec"}',
        headers={"content-type": "text/plain"},
    )
    assert res.status_code == 403
    assert len(node_repo.list_nodes(kind=NodeKind.SPEC)) == 1


def test_write_guard_refuses_foreign_origin(
    api: tuple[TestClient, NodeRepository, LedgerRepository],
) -> None:
    client, node_repo, _ledger_repo = api
    res = client.post(
        "/api/specs",
        json={"title": "New Spec"},
        headers={"origin": "http://evil.example"},
    )
    assert res.status_code == 403
    assert len(node_repo.list_nodes(kind=NodeKind.SPEC)) == 1


def test_write_guard_passes_same_host_origin(
    api: tuple[TestClient, NodeRepository, LedgerRepository],
) -> None:
    client, _node_repo, _ledger_repo = api
    res = client.post("/api/specs", json={"title": "New Spec"}, headers={"origin": SAME_ORIGIN})
    assert res.status_code == 201


def test_actor_defaults_to_web_and_reads_x_tm_actor_header(
    api: tuple[TestClient, NodeRepository, LedgerRepository],
) -> None:
    client, _node_repo, ledger_repo = api
    client.post("/api/specs", json={"title": "Default actor"})
    assert _last_actor(ledger_repo) == "web"
    client.post("/api/specs", json={"title": "Named actor"}, headers={"X-TM-Actor": "alice"})
    assert _last_actor(ledger_repo) == "alice"


# -- /api/meta --------------------------------------------------------------------------------


def test_get_meta_lists_pickers(
    api: tuple[TestClient, NodeRepository, LedgerRepository],
) -> None:
    client, _node_repo, _ledger_repo = api
    res = client.get("/api/meta")
    assert res.status_code == 200
    body = res.json()
    assert "READY" in body["statuses"]
    assert "NOT_STARTED" in body["statuses"]
    assert VerificationType.FILE_EXISTS.value in body["verification_types"]
    assert {"id": "SPEC", "title": "Spec"} in body["specs"]
    assert {"id": "SPEC-P1", "title": "Plan"} in body["plans"]


# -- specs / plans / tasks ---------------------------------------------------------------------


def test_create_spec(api: tuple[TestClient, NodeRepository, LedgerRepository]) -> None:
    client, node_repo, ledger_repo = api
    res = client.post("/api/specs", json={"title": "New Spec", "slug": "NEW", "priority": 70})
    assert res.status_code == 201
    assert res.json() == {"id": "NEW"}
    node = node_repo.get_node("NEW")
    assert node is not None
    assert node.priority == 70
    assert _last_actor(ledger_repo) == "web"


def test_create_plan(api: tuple[TestClient, NodeRepository, LedgerRepository]) -> None:
    client, node_repo, _ledger_repo = api
    res = client.post("/api/plans", json={"title": "New Plan", "spec": "SPEC", "slug": "P2"})
    assert res.status_code == 201
    assert res.json()["id"] == "SPEC-P2"
    assert node_repo.get_node("SPEC-P2") is not None


def test_create_task(api: tuple[TestClient, NodeRepository, LedgerRepository]) -> None:
    client, node_repo, _ledger_repo = api
    res = client.post(
        "/api/tasks",
        json={
            "title": "New Task",
            "plan": "SPEC-P1",
            "slug": "T3",
            "depends_on": ["SPEC-P1-T1"],
            "models": ["sonnet"],
        },
    )
    assert res.status_code == 201
    assert res.json()["id"] == "SPEC-P1-T3"
    task = node_repo.get_node("SPEC-P1-T3")
    assert task is not None
    assert task.acceptable_models == ["sonnet"]
    assert node_repo.get_dependencies("SPEC-P1-T3") == ["SPEC-P1-T1"]


# -- PATCH /api/nodes/{id} --------------------------------------------------------------------


def test_patch_node_updates_only_given_fields(
    api: tuple[TestClient, NodeRepository, LedgerRepository],
) -> None:
    client, node_repo, ledger_repo = api
    res = client.patch(
        "/api/nodes/SPEC-P1-T1",
        json={"priority": 90, "frontmatter_set": {"declared_files": ["a.py"]}},
    )
    assert res.status_code == 200
    node = node_repo.get_node("SPEC-P1-T1")
    assert node is not None
    assert node.priority == 90
    assert node.title == "Task"
    assert node.frontmatter["declared_files"] == ["a.py"]
    assert _last_actor(ledger_repo) == "web"


def test_patch_node_unknown_id_refused_and_writes_nothing(
    api: tuple[TestClient, NodeRepository, LedgerRepository],
) -> None:
    client, node_repo, _ledger_repo = api
    res = client.patch("/api/nodes/NOPE", json={"title": "x"})
    assert res.status_code == 404
    assert node_repo.get_node("NOPE") is None


def test_patch_node_nothing_to_update_refused(
    api: tuple[TestClient, NodeRepository, LedgerRepository],
) -> None:
    client, _node_repo, _ledger_repo = api
    res = client.patch("/api/nodes/SPEC-P1-T1", json={})
    assert res.status_code == 400
    assert "detail" in res.json()


# -- status -------------------------------------------------------------------------------------


def test_post_status_updates_node(
    api: tuple[TestClient, NodeRepository, LedgerRepository],
) -> None:
    client, node_repo, _ledger_repo = api
    res = client.post("/api/nodes/SPEC-P1-T1/status", json={"status": "ABANDONED"})
    assert res.status_code == 200
    node = node_repo.get_node("SPEC-P1-T1")
    assert node is not None
    assert node.status == NodeStatus.ABANDONED


def test_post_status_unknown_node_is_404_and_writes_nothing(
    api: tuple[TestClient, NodeRepository, LedgerRepository],
) -> None:
    client, node_repo, _ledger_repo = api
    res = client.post("/api/nodes/NOPE/status", json={"status": "ABANDONED"})
    assert res.status_code == 404
    assert node_repo.get_node("NOPE") is None


# -- dependencies ---------------------------------------------------------------------------


def test_post_dependencies_add_and_remove(
    api: tuple[TestClient, NodeRepository, LedgerRepository],
) -> None:
    client, node_repo, _ledger_repo = api
    res = client.post(
        "/api/nodes/SPEC-P1-T2/dependencies",
        json={"add": [{"id": "SPEC-P1-T1", "gate": "WAITING_REVIEW"}]},
    )
    assert res.status_code == 200
    assert res.json() == [{"id": "SPEC-P1-T1", "gate": "WAITING_REVIEW"}]
    assert node_repo.get_dependencies("SPEC-P1-T2") == ["SPEC-P1-T1"]

    res2 = client.post("/api/nodes/SPEC-P1-T2/dependencies", json={"remove": ["SPEC-P1-T1"]})
    assert res2.status_code == 200
    assert node_repo.get_dependencies("SPEC-P1-T2") == []


def test_post_dependencies_cycle_refused_and_writes_nothing(
    api: tuple[TestClient, NodeRepository, LedgerRepository],
) -> None:
    client, node_repo, _ledger_repo = api
    client.post("/api/nodes/SPEC-P1-T2/dependencies", json={"add": [{"id": "SPEC-P1-T1"}]})
    res = client.post("/api/nodes/SPEC-P1-T1/dependencies", json={"add": [{"id": "SPEC-P1-T2"}]})
    assert res.status_code == 409
    assert node_repo.get_dependencies("SPEC-P1-T1") == []


# -- supersede / move ------------------------------------------------------------------------


def test_post_supersede(api: tuple[TestClient, NodeRepository, LedgerRepository]) -> None:
    client, node_repo, _ledger_repo = api
    res = client.post("/api/nodes/SPEC-P1-T1/supersede", json={"by": "SPEC-P1-T2"})
    assert res.status_code == 200
    node = node_repo.get_node("SPEC-P1-T1")
    assert node is not None
    assert node.status == NodeStatus.SUPERSEDED


def test_post_supersede_unknown_replacement_refused_and_writes_nothing(
    api: tuple[TestClient, NodeRepository, LedgerRepository],
) -> None:
    client, node_repo, _ledger_repo = api
    res = client.post("/api/nodes/SPEC-P1-T1/supersede", json={"by": "NOPE"})
    assert res.status_code == 400
    node = node_repo.get_node("SPEC-P1-T1")
    assert node is not None
    assert node.status == NodeStatus.NOT_STARTED


def test_post_move(api: tuple[TestClient, NodeRepository, LedgerRepository]) -> None:
    client, node_repo, _ledger_repo = api
    node_repo.save_node(Node(id="SPEC-P2", kind=NodeKind.PLAN, title="Other plan"))
    node_repo.add_relation(
        NodeRelation(source_id="SPEC", target_id="SPEC-P2", relation_type=RelationType.CONTAINS)
    )
    res = client.post("/api/nodes/SPEC-P1-T1/move", json={"plan": "SPEC-P2"})
    assert res.status_code == 200
    assert "SPEC-P1-T1" in node_repo.get_children("SPEC-P2")
    assert "SPEC-P1-T1" not in node_repo.get_children("SPEC-P1")


def test_post_move_unknown_plan_refused_and_writes_nothing(
    api: tuple[TestClient, NodeRepository, LedgerRepository],
) -> None:
    client, node_repo, _ledger_repo = api
    res = client.post("/api/nodes/SPEC-P1-T1/move", json={"plan": "NOPE"})
    assert res.status_code == 404
    assert "SPEC-P1-T1" in node_repo.get_children("SPEC-P1")


# -- sections ---------------------------------------------------------------------------------


def test_put_and_delete_section(
    api: tuple[TestClient, NodeRepository, LedgerRepository],
) -> None:
    client, node_repo, _ledger_repo = api
    res = client.put(
        "/api/nodes/SPEC-P1-T1/sections/notes", json={"content": "hello", "header": "## Notes"}
    )
    assert res.status_code == 200
    sec = node_repo.get_section("SPEC-P1-T1", "notes")
    assert sec is not None
    assert sec.content == "hello"

    res2 = client.delete("/api/nodes/SPEC-P1-T1/sections/notes", headers=JSON)
    assert res2.status_code == 200
    assert node_repo.get_section("SPEC-P1-T1", "notes") is None


def test_delete_section_missing_is_404_and_writes_nothing(
    api: tuple[TestClient, NodeRepository, LedgerRepository],
) -> None:
    client, node_repo, _ledger_repo = api
    res = client.delete("/api/nodes/SPEC-P1-T1/sections/nope", headers=JSON)
    assert res.status_code == 404
    assert node_repo.get_all_sections("SPEC-P1-T1") == []


# -- verifications ----------------------------------------------------------------------------


def test_post_and_delete_verification(
    api: tuple[TestClient, NodeRepository, LedgerRepository],
) -> None:
    client, node_repo, _ledger_repo = api
    res = client.post(
        "/api/nodes/SPEC-P1-T1/verifications",
        json={"type": "file_exists", "target_path": "src/a.py"},
    )
    assert res.status_code == 201
    ver_id = res.json()["id"]
    assert len(node_repo.get_verifications("SPEC-P1-T1")) == 1

    res2 = client.delete(f"/api/nodes/SPEC-P1-T1/verifications/{ver_id}", headers=JSON)
    assert res2.status_code == 200
    assert node_repo.get_verifications("SPEC-P1-T1") == []


def test_delete_verification_missing_is_404_and_writes_nothing(
    api: tuple[TestClient, NodeRepository, LedgerRepository],
) -> None:
    client, node_repo, _ledger_repo = api
    res = client.delete("/api/nodes/SPEC-P1-T1/verifications/999", headers=JSON)
    assert res.status_code == 404
    assert node_repo.get_verifications("SPEC-P1-T1") == []


def test_post_verify_runs_and_reports_per_row(
    api: tuple[TestClient, NodeRepository, LedgerRepository],
) -> None:
    client, node_repo, _ledger_repo = api
    node_repo.add_verification(
        NodeVerification(
            node_id="SPEC-P1-T1",
            verification_type=VerificationType.FILE_EXISTS,
            target_path="does/not/exist.py",
        )
    )
    res = client.post("/api/nodes/SPEC-P1-T1/verify", headers=JSON)
    assert res.status_code == 200
    rows = res.json()
    assert len(rows) == 1
    assert rows[0]["passed"] is False
    assert rows[0]["type"] == "file_exists"


def test_post_verify_empty_set_refused(
    api: tuple[TestClient, NodeRepository, LedgerRepository],
) -> None:
    client, _node_repo, _ledger_repo = api
    res = client.post("/api/nodes/SPEC-P1-T1/verify", headers=JSON)
    assert res.status_code == 400


# -- lease / sweep ----------------------------------------------------------------------------


def test_delete_lease_releases_it(
    api: tuple[TestClient, NodeRepository, LedgerRepository],
) -> None:
    client, _node_repo, _ledger_repo = api
    res = client.delete("/api/nodes/SPEC-P1-T1/lease", headers=JSON)
    assert res.status_code == 200


def test_post_sweep_returns_swept_list(
    api: tuple[TestClient, NodeRepository, LedgerRepository],
) -> None:
    client, _node_repo, _ledger_repo = api
    res = client.post("/api/leases/sweep", headers=JSON)
    assert res.status_code == 200
    assert res.json() == {"swept": []}
