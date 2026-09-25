"""Integration tests for the write API (§5), decisions (§3), attachments and file serving (§4)."""

import base64
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from taskmanager.core.enums import NodeKind, RelationType, VerificationType
from taskmanager.core.models import Lease, Node, NodeRelation, NodeVerification
from taskmanager.core.status import Action, DecisionStatus, Status
from taskmanager.db.connection import DatabaseManager
from taskmanager.db.ledger_repo import LedgerRepository
from taskmanager.db.node_repo import NodeRepository
from taskmanager.engine.snapshot import stored_status
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
    node_repo.save_node(Node(id="SPEC", kind=NodeKind.SPEC, title="Spec", status=Status.READY))
    node_repo.save_node(Node(id="SPEC-P1", kind=NodeKind.PLAN, title="Plan", status=Status.READY))
    node_repo.add_relation(
        NodeRelation(source_id="SPEC", target_id="SPEC-P1", relation_type=RelationType.CONTAINS)
    )
    node_repo.save_node(
        Node(id="SPEC-P1-T1", kind=NodeKind.TASK, title="Task", priority=60, status=Status.READY)
    )
    node_repo.add_relation(
        NodeRelation(
            source_id="SPEC-P1", target_id="SPEC-P1-T1", relation_type=RelationType.CONTAINS
        )
    )
    node_repo.save_node(
        Node(id="SPEC-P1-T2", kind=NodeKind.TASK, title="Second", status=Status.READY)
    )
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
    assert "NOT_STARTED" not in body["statuses"]
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


# -- dependencies ---------------------------------------------------------------------------


def test_post_dependencies_add_and_remove(
    api: tuple[TestClient, NodeRepository, LedgerRepository],
) -> None:
    client, node_repo, _ledger_repo = api
    res = client.post("/api/nodes/SPEC-P1-T2/dependencies", json={"add": [{"id": "SPEC-P1-T1"}]})
    assert res.status_code == 200
    assert res.json() == [{"id": "SPEC-P1-T1"}]
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
    assert node.status == Status.SUPERSEDED


def test_post_supersede_unknown_replacement_refused_and_writes_nothing(
    api: tuple[TestClient, NodeRepository, LedgerRepository],
) -> None:
    client, node_repo, _ledger_repo = api
    res = client.post("/api/nodes/SPEC-P1-T1/supersede", json={"by": "NOPE"})
    assert res.status_code == 400
    node = node_repo.get_node("SPEC-P1-T1")
    assert node is not None
    assert node.status == Status.READY


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


def test_delete_lease_gives_the_claimed_step_back(
    api: tuple[TestClient, NodeRepository, LedgerRepository],
) -> None:
    client, node_repo, _ledger_repo = api
    node = node_repo.get_node("SPEC-P1-T1")
    assert node is not None
    node.status, node.claimed_from = Status.IMPLEMENTING, Status.READY
    lease = Lease(
        task_id="SPEC-P1-T1",
        agent_id="a",
        session_id="s",
        branch_name="tm/SPEC-P1-T1",
        action=Action.IMPLEMENT,
        ttl_seconds=3600,
    )
    from taskmanager.db.runtime_repo import RuntimeRepository

    assert RuntimeRepository(node_repo.db).claim(lease, [], node)
    res = client.delete("/api/nodes/SPEC-P1-T1/lease", headers=JSON)
    assert res.status_code == 200
    assert res.json() == {"id": "SPEC-P1-T1", "status": "READY"}


def test_post_sweep_returns_swept_list(
    api: tuple[TestClient, NodeRepository, LedgerRepository],
) -> None:
    client, _node_repo, _ledger_repo = api
    res = client.post("/api/leases/sweep", headers=JSON)
    assert res.status_code == 200
    assert res.json() == {"swept": []}


# -- decisions ----------------------------------------------------------------------------------


def test_create_decision_with_options_and_blocks(
    api: tuple[TestClient, NodeRepository, LedgerRepository],
) -> None:
    client, node_repo, ledger_repo = api
    res = client.post(
        "/api/decisions",
        json={
            "question": "Which auth flow?",
            "slug": "auth-flow",
            "options": [
                {"key": "a", "label": "Session cookies"},
                {"key": "b", "label": "JWT", "description": "stateless"},
            ],
            "recommend": "b",
            "blocks": ["SPEC-P1-T1"],
        },
    )
    assert res.status_code == 201
    decision_id = res.json()["id"]
    assert decision_id == "decision-auth-flow"
    node = node_repo.get_node(decision_id)
    assert node is not None
    assert node.kind == NodeKind.DECISION
    assert node.frontmatter["decision"]["options"][1]["recommended"] is True
    assert "decision-auth-flow" in node_repo.get_dependencies("SPEC-P1-T1")
    assert _last_actor(ledger_repo) == "web"


def test_create_decision_unknown_blocked_task_refused_and_writes_nothing(
    api: tuple[TestClient, NodeRepository, LedgerRepository],
) -> None:
    client, node_repo, _ledger_repo = api
    res = client.post("/api/decisions", json={"question": "Q?", "blocks": ["NOPE"]})
    assert res.status_code == 404
    assert node_repo.list_nodes(kind=NodeKind.DECISION) == []


def _seed_decision(node_repo: NodeRepository, decision_id: str = "decision-D1") -> None:
    node_repo.save_node(
        Node(
            id=decision_id,
            kind=NodeKind.DECISION,
            title="Which auth flow?",
            frontmatter={
                "decision": {
                    "options": [{"key": "a", "label": "Cookies", "recommended": False}],
                    "allow_custom": True,
                    "raised_by": None,
                    "answer": None,
                    "withdrawn_reason": "",
                }
            },
        )
    )


def test_answer_decision_unblocks_dependent_task(
    api: tuple[TestClient, NodeRepository, LedgerRepository],
) -> None:
    client, node_repo, _ledger_repo = api
    _seed_decision(node_repo)
    node_repo.add_relation(
        NodeRelation(
            source_id="SPEC-P1-T1",
            target_id="decision-D1",
            relation_type=RelationType.DEPENDS_ON,
        )
    )
    res = client.post(
        "/api/decisions/decision-D1/answer",
        json={"option": "a", "rationale": "simplest"},
        headers={"X-TM-Actor": "owner"},
    )
    assert res.status_code == 200
    node = node_repo.get_node("decision-D1")
    assert node is not None
    assert node.status == DecisionStatus.ANSWERED
    assert node.frontmatter["decision"]["answer"]["answered_by"] == "owner"


def test_answer_decision_unknown_option_refused_and_writes_nothing(
    api: tuple[TestClient, NodeRepository, LedgerRepository],
) -> None:
    client, node_repo, _ledger_repo = api
    _seed_decision(node_repo)
    res = client.post("/api/decisions/decision-D1/answer", json={"option": "nope"})
    assert res.status_code == 400
    node = node_repo.get_node("decision-D1")
    assert node is not None
    assert stored_status(node) == DecisionStatus.OPEN


def test_reopen_and_withdraw_decision(
    api: tuple[TestClient, NodeRepository, LedgerRepository],
) -> None:
    client, node_repo, _ledger_repo = api
    _seed_decision(node_repo)
    client.post("/api/decisions/decision-D1/answer", json={"option": "a"})
    res = client.post("/api/decisions/decision-D1/reopen", headers=JSON)
    assert res.status_code == 200
    node = node_repo.get_node("decision-D1")
    assert node is not None
    assert node.status == DecisionStatus.OPEN

    res2 = client.post("/api/decisions/decision-D1/withdraw", json={"reason": "no longer relevant"})
    assert res2.status_code == 200
    node2 = node_repo.get_node("decision-D1")
    assert node2 is not None
    assert node2.status == DecisionStatus.WITHDRAWN


def test_decision_blocks_add_and_remove(
    api: tuple[TestClient, NodeRepository, LedgerRepository],
) -> None:
    client, node_repo, _ledger_repo = api
    _seed_decision(node_repo)
    res = client.post(
        "/api/decisions/decision-D1/blocks", json={"add": ["SPEC-P1-T1", "SPEC-P1-T2"]}
    )
    assert res.status_code == 200
    assert set(node_repo.get_blocked_by("decision-D1")) == {"SPEC-P1-T1", "SPEC-P1-T2"}

    res2 = client.post("/api/decisions/decision-D1/blocks", json={"remove": ["SPEC-P1-T1"]})
    assert res2.status_code == 200
    assert node_repo.get_blocked_by("decision-D1") == ["SPEC-P1-T2"]


def test_list_decisions_filters_by_status_tab(
    api: tuple[TestClient, NodeRepository, LedgerRepository],
) -> None:
    client, node_repo, _ledger_repo = api
    _seed_decision(node_repo, "decision-D1")
    _seed_decision(node_repo, "decision-D2")
    client.post("/api/decisions/decision-D2/answer", json={"option": "a"})

    res_open = client.get("/api/decisions", params={"status": "open"})
    assert res_open.status_code == 200
    assert [d["id"] for d in res_open.json()] == ["decision-D1"]

    res_answered = client.get("/api/decisions", params={"status": "answered"})
    assert [d["id"] for d in res_answered.json()] == ["decision-D2"]

    res_bad = client.get("/api/decisions", params={"status": "bogus"})
    assert res_bad.status_code == 400


# -- attachments ----------------------------------------------------------------------------


_PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


def test_post_attachment_stores_content_addressed_asset(
    api: tuple[TestClient, NodeRepository, LedgerRepository],
) -> None:
    client, node_repo, ledger_repo = api
    res = client.post(
        "/api/nodes/SPEC-P1-T1/attachments",
        json={
            "filename": "shot.png",
            "content_base64": base64.b64encode(_PNG_1PX).decode("ascii"),
            "caption": "before",
            "source": "shot.png",
        },
    )
    assert res.status_code == 201
    entry = res.json()
    assert entry["name"] == "shot.png"
    assert entry["mime"] == "image/png"
    node = node_repo.get_node("SPEC-P1-T1")
    assert node is not None
    assert node.frontmatter["attachments"] == [entry]
    assert _last_actor(ledger_repo) == "web"

    asset_res = client.get(f"/assets/{entry['asset']}")
    assert asset_res.status_code == 200
    assert asset_res.content == _PNG_1PX


def test_post_attachment_unknown_node_refused_and_writes_nothing(
    api: tuple[TestClient, NodeRepository, LedgerRepository],
) -> None:
    client, node_repo, _ledger_repo = api
    res = client.post(
        "/api/nodes/NOPE/attachments",
        json={"filename": "shot.png", "content_base64": base64.b64encode(_PNG_1PX).decode()},
    )
    assert res.status_code == 404
    assert node_repo.get_node("NOPE") is None


def test_post_attachment_bad_base64_refused(
    api: tuple[TestClient, NodeRepository, LedgerRepository],
) -> None:
    client, _node_repo, _ledger_repo = api
    res = client.post(
        "/api/nodes/SPEC-P1-T1/attachments",
        json={"filename": "shot.png", "content_base64": "not-base64!!"},
    )
    assert res.status_code == 400


def test_post_attachment_dotdot_filename_refuses_400_not_500(
    api: tuple[TestClient, NodeRepository, LedgerRepository],
) -> None:
    # `Path("..").name` is "..", not "" -- joining that onto the temp dir resolves to the
    # dir itself, and write_bytes there raised an uncaught IsADirectoryError.
    client, _node_repo, _ledger_repo = api
    res = client.post(
        "/api/nodes/SPEC-P1-T1/attachments",
        json={"filename": "..", "content_base64": "aGk="},
    )
    assert res.status_code == 400


def test_attachment_check_marks_stale_and_missing(
    api: tuple[TestClient, NodeRepository, LedgerRepository], tmp_path: Path
) -> None:
    client, node_repo, _ledger_repo = api
    source = tmp_path / "shot.png"
    source.write_bytes(_PNG_1PX)
    client.post(
        "/api/nodes/SPEC-P1-T1/attachments",
        json={
            "filename": "shot.png",
            "content_base64": base64.b64encode(_PNG_1PX).decode(),
            "source": "shot.png",
        },
    )
    node = node_repo.get_node("SPEC-P1-T1")
    assert node is not None
    assert node.frontmatter["attachments"][0]["source"]["state"] == "fresh"

    source.write_bytes(_PNG_1PX + b"\x00")
    res = client.post("/api/nodes/SPEC-P1-T1/attachments/check", headers=JSON)
    assert res.status_code == 200
    assert res.json()[0]["source"]["state"] == "stale"

    source.unlink()
    res2 = client.post("/api/nodes/SPEC-P1-T1/attachments/check", headers=JSON)
    assert res2.json()[0]["source"]["state"] == "missing"


def test_delete_attachment_removes_entry(
    api: tuple[TestClient, NodeRepository, LedgerRepository],
) -> None:
    client, node_repo, _ledger_repo = api
    res = client.post(
        "/api/nodes/SPEC-P1-T1/attachments",
        json={"filename": "shot.png", "content_base64": base64.b64encode(_PNG_1PX).decode()},
    )
    asset = res.json()["asset"]
    res2 = client.delete(f"/api/nodes/SPEC-P1-T1/attachments/{asset}", headers=JSON)
    assert res2.status_code == 200
    node = node_repo.get_node("SPEC-P1-T1")
    assert node is not None
    assert node.frontmatter.get("attachments") == []


def test_delete_attachment_missing_is_404(
    api: tuple[TestClient, NodeRepository, LedgerRepository],
) -> None:
    client, _node_repo, _ledger_repo = api
    res = client.delete("/api/nodes/SPEC-P1-T1/attachments/nope.png", headers=JSON)
    assert res.status_code == 404


# -- attachment size_bytes: read from the stored asset, in every place attachments render ---


def _find_tree_node(tree: list[dict], node_id: str) -> dict:
    for n in tree:
        if n["id"] == node_id:
            return n
        found = _find_tree_node(n.get("children", []), node_id)
        if found is not None:
            return found
    raise AssertionError(f"{node_id} not in tree")


def test_tree_and_node_detail_carry_attachment_size_bytes(
    api: tuple[TestClient, NodeRepository, LedgerRepository], tmp_path: Path
) -> None:
    client, _node_repo, _ledger_repo = api
    up = client.post(
        "/api/nodes/SPEC-P1-T1/attachments",
        json={"filename": "shot.png", "content_base64": base64.b64encode(_PNG_1PX).decode()},
    )
    asset = up.json()["asset"]

    tree = client.get("/api/tree").json()
    task = _find_tree_node(tree, "SPEC-P1-T1")
    assert task["frontmatter"]["attachments"][0]["size_bytes"] == len(_PNG_1PX)

    detail = client.get("/api/nodes/SPEC-P1-T1").json()
    assert detail["node"]["frontmatter"]["attachments"][0]["size_bytes"] == len(_PNG_1PX)

    # The stored asset gone (a manual delete outside `detach`) reads as null, not an error.
    (tmp_path / ".taskmanager" / "assets" / asset).unlink()
    detail2 = client.get("/api/nodes/SPEC-P1-T1").json()
    assert detail2["node"]["frontmatter"]["attachments"][0]["size_bytes"] is None


def test_decisions_list_carries_attachment_size_bytes(
    api: tuple[TestClient, NodeRepository, LedgerRepository],
) -> None:
    client, _node_repo, _ledger_repo = api
    decision_id = client.post("/api/decisions", json={"question": "Q?", "slug": "q1"}).json()["id"]
    up = client.post(
        f"/api/nodes/{decision_id}/attachments",
        json={"filename": "shot.png", "content_base64": base64.b64encode(_PNG_1PX).decode()},
    )
    assert up.status_code == 201

    decisions = client.get("/api/decisions").json()
    row = next(d for d in decisions if d["id"] == decision_id)
    assert row["attachments"][0]["size_bytes"] == len(_PNG_1PX)


# -- /assets/{name} and /api/file: read-only, path traversal refused ------------------------


def test_get_asset_malformed_name_is_404(
    api: tuple[TestClient, NodeRepository, LedgerRepository],
) -> None:
    client, _node_repo, _ledger_repo = api
    res = client.get("/assets/not-a-valid-name.png")
    assert res.status_code == 404


def test_get_asset_traversal_is_404(
    api: tuple[TestClient, NodeRepository, LedgerRepository],
) -> None:
    client, _node_repo, _ledger_repo = api
    res = client.get("/assets/..%2f..%2fetc%2fpasswd")
    assert res.status_code in (404, 400)


def test_get_file_serves_an_image_inside_root(
    api: tuple[TestClient, NodeRepository, LedgerRepository], tmp_path: Path
) -> None:
    client, _node_repo, _ledger_repo = api
    (tmp_path / "shot.png").write_bytes(_PNG_1PX)
    res = client.get("/api/file", params={"path": "shot.png"})
    assert res.status_code == 200
    assert res.content == _PNG_1PX


def test_get_file_refuses_traversal_outside_root(
    api: tuple[TestClient, NodeRepository, LedgerRepository], tmp_path: Path
) -> None:
    client, _node_repo, _ledger_repo = api
    outside = tmp_path.parent / "outside.png"
    outside.write_bytes(_PNG_1PX)
    res = client.get("/api/file", params={"path": "../outside.png"})
    assert res.status_code == 404
    outside.unlink()


def test_get_file_refuses_absolute_path_outside_root(
    api: tuple[TestClient, NodeRepository, LedgerRepository], tmp_path: Path
) -> None:
    client, _node_repo, _ledger_repo = api
    outside = tmp_path.parent / "outside2.png"
    outside.write_bytes(_PNG_1PX)
    res = client.get("/api/file", params={"path": str(outside)})
    assert res.status_code == 404
    outside.unlink()


def test_get_file_refuses_symlink_escaping_root(
    api: tuple[TestClient, NodeRepository, LedgerRepository], tmp_path: Path
) -> None:
    client, _node_repo, _ledger_repo = api
    outside = tmp_path.parent / "outside3.png"
    outside.write_bytes(_PNG_1PX)
    link = tmp_path / "escape.png"
    link.symlink_to(outside)
    try:
        res = client.get("/api/file", params={"path": "escape.png"})
        assert res.status_code == 404
    finally:
        link.unlink()
        outside.unlink()


def test_get_file_refuses_non_image(
    api: tuple[TestClient, NodeRepository, LedgerRepository], tmp_path: Path
) -> None:
    client, _node_repo, _ledger_repo = api
    (tmp_path / "notes.txt").write_text("hello", encoding="utf-8")
    res = client.get("/api/file", params={"path": "notes.txt"})
    assert res.status_code == 404


# -- served files carry headers that stop them running as same-origin documents -------------


def test_get_asset_html_attachment_downloads_rather_than_renders(
    api: tuple[TestClient, NodeRepository, LedgerRepository], tmp_path: Path
) -> None:
    client, _node_repo, _ledger_repo = api
    assets_dir = tmp_path / ".taskmanager" / "assets"
    assets_dir.mkdir(parents=True, exist_ok=True)
    name = "0123456789abcdef.html"
    (assets_dir / name).write_text("<script>window.__x=1</script>", encoding="utf-8")

    res = client.get(f"/assets/{name}")

    assert res.status_code == 200
    assert res.headers["x-content-type-options"] == "nosniff"
    assert res.headers["content-security-policy"] == "sandbox"
    assert res.headers["content-disposition"].startswith("attachment")


def test_get_asset_image_is_inline_but_still_sandboxed(
    api: tuple[TestClient, NodeRepository, LedgerRepository], tmp_path: Path
) -> None:
    client, _node_repo, _ledger_repo = api
    assets_dir = tmp_path / ".taskmanager" / "assets"
    assets_dir.mkdir(parents=True, exist_ok=True)
    name = "fedcba9876543210.png"
    (assets_dir / name).write_bytes(_PNG_1PX)

    res = client.get(f"/assets/{name}")

    assert res.status_code == 200
    assert res.headers["x-content-type-options"] == "nosniff"
    assert res.headers["content-security-policy"] == "sandbox"
    assert res.headers["content-disposition"].startswith("inline")


def test_get_file_svg_is_still_sandboxed_against_a_direct_open(
    api: tuple[TestClient, NodeRepository, LedgerRepository], tmp_path: Path
) -> None:
    client, _node_repo, _ledger_repo = api
    (tmp_path / "icon.svg").write_text(
        "<svg xmlns='http://www.w3.org/2000/svg'><script>window.__x=1</script></svg>",
        encoding="utf-8",
    )
    res = client.get("/api/file", params={"path": "icon.svg"})
    assert res.status_code == 200
    assert res.headers["x-content-type-options"] == "nosniff"
    assert res.headers["content-security-policy"] == "sandbox"


# -- Host pinning: DNS rebinding sends a matching Origin and Host, neither the real one -------


def test_write_guard_pins_host_against_dns_rebinding(tmp_path: Path) -> None:
    db_mgr = DatabaseManager(tmp_path / ".taskmanager")
    db_mgr.init_all()
    # The real server: bound to 127.0.0.1:6701, as `tm web run` would call it.
    app = create_app(tmp_path, host="127.0.0.1", port=6701)
    client = TestClient(app)

    # A rebinding attacker's page resolves its own domain to 127.0.0.1, so the browser sends
    # that domain in *both* Host and Origin -- comparing them to each other (the old guard)
    # cannot tell this apart from a legitimate same-origin request.
    rebind = "rebind.attacker.example:6701"
    res = client.post(
        "/api/specs",
        json={"title": "pwned"},
        headers={"host": rebind, "origin": f"http://{rebind}"},
    )
    assert res.status_code == 403

    ok = client.post(
        "/api/specs",
        json={"title": "legit"},
        headers={"host": "127.0.0.1:6701", "origin": "http://127.0.0.1:6701"},
    )
    assert ok.status_code == 201


def test_write_guard_accepts_localhost_alias_for_a_loopback_bind(tmp_path: Path) -> None:
    db_mgr = DatabaseManager(tmp_path / ".taskmanager")
    db_mgr.init_all()
    app = create_app(tmp_path, host="127.0.0.1", port=6701)
    client = TestClient(app)
    res = client.post(
        "/api/specs",
        json={"title": "via localhost"},
        headers={"host": "localhost:6701", "origin": "http://localhost:6701"},
    )
    assert res.status_code == 201


# -- static_export.py: an attacker-writable attachment name cannot escape assets_dir ---------


def test_static_export_ignores_a_traversal_asset_name(tmp_path: Path) -> None:
    from taskmanager.core.models import Node
    from taskmanager.web.static_export import export_static_html

    db_mgr = DatabaseManager(tmp_path / ".taskmanager")
    db_mgr.init_all()
    # `init_all()` does not create `assets/` (only the first real attach does); the OS still
    # needs every directory the ".." walk passes through to exist, or the traversal attempt
    # itself 404s before the guard under test is ever reached, and the test proves nothing.
    (tmp_path / ".taskmanager" / "assets").mkdir(parents=True, exist_ok=True)
    node_repo = NodeRepository(db_mgr)
    secret_dir = tmp_path.parent / "outside-secret"
    secret_dir.mkdir(exist_ok=True)
    (secret_dir / "secret.png").write_bytes(b"PNG-SECRET-BYTES")
    node_repo.save_node(
        Node(
            id="T1",
            kind=NodeKind.TASK,
            title="Task",
            frontmatter={
                "attachments": [
                    {
                        # assets_dir is <project_root>/.taskmanager/assets, so it takes three
                        # ".." segments to actually reach a sibling of project_root itself.
                        "asset": "../../../outside-secret/secret.png",
                        "name": "secret.png",
                        "caption": "",
                        "mime": "image/png",
                    }
                ]
            },
        )
    )
    try:
        out = export_static_html(tmp_path, tmp_path / "out.html")
        html = out.read_text(encoding="utf-8")
        assert b"PNG-SECRET-BYTES".decode() not in html
        assert "SECRET" not in html
        # A successful escape embeds the file as a base64 data URI; the plaintext checks above
        # never see that (it is base64), so this is the assertion an unguarded traversal trips.
        assert "data:image/png;base64" not in html
    finally:
        (secret_dir / "secret.png").unlink()
        secret_dir.rmdir()
