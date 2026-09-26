"""Integration tests for the TaskManager web visualizer and CLI commands."""

import base64
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from taskmanager.cli.main import _find_available_port, app
from taskmanager.core.enums import NodeKind, RelationType, VerificationType
from taskmanager.core.models import (
    FileLock,
    Lease,
    Node,
    NodeRelation,
    NodeSection,
    NodeVerification,
)
from taskmanager.core.status import Action, DecisionStatus, Outcome, Status
from taskmanager.db.connection import DatabaseManager
from taskmanager.db.node_repo import NodeRepository
from taskmanager.db.runtime_repo import RuntimeRepository
from taskmanager.web.app import create_app
from taskmanager.web.static_export import export_static_html

runner = CliRunner()

_PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


def test_web_api_endpoints_and_ui(tmp_path: Path) -> None:
    db_mgr = DatabaseManager(tmp_path / ".taskmanager")
    db_mgr.init_all()
    node_repo = NodeRepository(db_mgr)

    # Add sample spec, plan, task
    node_repo.save_node(Node(id="AUTH", kind=NodeKind.SPEC, title="Authentication Spec"))
    node_repo.save_node(Node(id="AUTH-P1", kind=NodeKind.PLAN, title="JWT Auth Plan"))
    node_repo.save_node(
        Node(
            id="AUTH-T1",
            kind=NodeKind.TASK,
            title="Implement Token Verification",
            status=Status.READY,
            priority=85,
            acceptable_models=["claude-3-7-sonnet"],
        )
    )
    node_repo.save_section(
        NodeSection(
            node_id="AUTH-T1",
            section_key="steps",
            ordinal=1,
            header="## Steps",
            content="- [ ] Step 1: Write test",
        )
    )
    node_repo.add_verification(
        NodeVerification(
            node_id="AUTH-T1",
            verification_type=VerificationType.FILE_EXISTS,
            target_path="src/auth/jwt.py",
        )
    )

    fastapi_app = create_app(tmp_path)
    client = TestClient(fastapi_app)

    # 1. Index HTML
    res_index = client.get("/")
    assert res_index.status_code == 200
    assert "TaskManager — Interactive Visualizer" in res_index.text

    # 2. /api/statuses
    res_statuses = client.get("/api/statuses")
    assert res_statuses.status_code == 200
    statuses_data = res_statuses.json()
    assert isinstance(statuses_data["hash"], str)
    root_entry = next(e for e in statuses_data["statuses"] if e["spec"] is None)
    root_plan = next(p for p in root_entry["plans"] if p["plan"] is None)
    assert root_plan["counts"]["READY"] >= 1

    # 3. /api/nodes
    res_nodes = client.get("/api/nodes", params={"ids": "AUTH,AUTH-T1"})
    assert res_nodes.status_code == 200
    node_ids = {n["id"] for n in res_nodes.json()["items"]}
    assert node_ids == {"AUTH", "AUTH-T1"}

    # 4. /api/nodes/{id}
    res_node = client.get("/api/nodes/AUTH-T1")
    assert res_node.status_code == 200
    node_detail = res_node.json()
    assert node_detail["node"]["id"] == "AUTH-T1"
    assert node_detail["node"]["priority"] == 85
    assert node_detail["display"] == "READY"
    assert len(node_detail["sections"]) == 1
    assert node_detail["sections"][0]["header"] == "## Steps"
    assert len(node_detail["verifications"]) == 1

    # 5. /api/nodes/nonexistent -> 404
    res_404 = client.get("/api/nodes/NONEXISTENT")
    assert res_404.status_code == 404

    # 6. WebSocket connection
    with client.websocket_connect("/ws") as ws:
        # Connection established successfully
        assert ws is not None


def test_static_html_export(tmp_path: Path) -> None:
    db_mgr = DatabaseManager(tmp_path / ".taskmanager")
    db_mgr.init_all()
    node_repo = NodeRepository(db_mgr)
    node_repo.save_node(Node(id="AUTH", kind=NodeKind.SPEC, title="Auth Spec"))

    out_file = tmp_path / "dashboard.html"
    result_path = export_static_html(tmp_path, out_file)
    assert result_path.exists()
    content = result_path.read_text(encoding="utf-8")
    assert "window.STATIC_DATA" in content
    assert "AUTH" in content


def test_static_export_embeds_a_sections_own_markdown_image(tmp_path: Path) -> None:
    # ![capture](evidence/x.png) renders client-side from the raw markdown (marked.parse), so
    # a relative path resolves against nothing once the file is opened from anywhere but the
    # exact export location -- the static export has no server left to serve it either.
    db_mgr = DatabaseManager(tmp_path / ".taskmanager")
    db_mgr.init_all()
    node_repo = NodeRepository(db_mgr)
    node_repo.save_node(Node(id="AUTH", kind=NodeKind.SPEC, title="Auth Spec"))
    (tmp_path / "evidence").mkdir()
    (tmp_path / "evidence" / "x.png").write_bytes(_PNG_1PX)
    node_repo.save_section(
        NodeSection(
            node_id="AUTH",
            section_key="evidence",
            ordinal=1,
            header="## Evidence",
            content="![capture](evidence/x.png)",
        )
    )

    out_file = tmp_path / "dashboard.html"
    content = export_static_html(tmp_path, out_file).read_text(encoding="utf-8")

    # The section's own content is what tree.js/detail.js render as markdown client side.
    static_match = re.search(r"window.STATIC_DATA = (\{.*?\});</script>", content)
    assert static_match
    static_data = json.loads(static_match.group(1))
    section = static_data["bodies"]["AUTH"]["sections"][0]
    assert (
        section["content"]
        == "![capture](data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=)"
    )


def test_find_available_port() -> None:
    port = _find_available_port("127.0.0.1", 6701)
    assert isinstance(port, int)
    assert port >= 6701


def test_cli_web_export_command(tmp_path: Path) -> None:
    # Initialize repo
    res_init = runner.invoke(app, ["init", "--path", str(tmp_path)])
    assert res_init.exit_code == 0

    out_file = tmp_path / "exported.html"
    res_export = runner.invoke(app, ["web", "export", "-o", str(out_file), "--path", str(tmp_path)])
    assert res_export.exit_code == 0
    assert "Exported static HTML visualizer" in res_export.stdout
    assert out_file.exists()


def test_cli_web_uninitialized_error(tmp_path: Path) -> None:
    empty_dir = tmp_path / "empty_proj"
    empty_dir.mkdir()
    res = runner.invoke(app, ["web", "export", "--path", str(empty_dir)])
    assert res.exit_code != 0
    assert "not initialized" in res.stdout


SEEDED_DISPLAY = {
    "T-READY": "READY",
    "T-IMPLEMENTING": "IMPLEMENTING",
    "T-STALE": "STALE",
    "T-WAITING-REVIEW": "WAITING_REVIEW",
    "T-WAITING-FIX": "WAITING_FIX",
    "T-WAITING-MERGE": "WAITING_MERGE",
    "T-COMPLETED": "COMPLETED",
    "T-FAILED": "FAILED",
    "T-DEFERRED": "DEFERRED",
    "T-ABANDONED": "ABANDONED",
    "T-SUPERSEDED": "SUPERSEDED",
    "T-AWAITING-DECISION": "AWAITING_DECISION",
    "T-BLOCKED": "BLOCKED_BY_TASK",
    "T-BLOCKED-BY-LEASE": "BLOCKED_BY_LEASE",
}


@pytest.fixture
def every_display_project(tmp_path: Path) -> Path:
    db_mgr = DatabaseManager(tmp_path / ".taskmanager")
    db_mgr.init_all()
    node_repo = NodeRepository(db_mgr)
    for node_id, kind in (("SPEC", NodeKind.SPEC), ("PLAN", NodeKind.PLAN)):
        node_repo.save_node(
            Node(
                id=node_id,
                kind=kind,
                title=node_id.title(),
                status=Status.READY,
                review=False,
                fix=False,
            )
        )
    node_repo.add_relation(
        NodeRelation(source_id="SPEC", target_id="PLAN", relation_type=RelationType.CONTAINS)
    )
    shared_path = "src/shared/module.py"

    def add_task(task_id: str, status: Status, repo: str = "core", **fields: Any) -> None:
        node_repo.save_node(
            Node(
                id=task_id,
                kind=NodeKind.TASK,
                title=f"Task {task_id}",
                status=status,
                review=True,
                fix=True,
                target_repo=repo,
                acceptable_models=["claude-opus-5"] if repo == "core" else ["gemini-flash"],
                **fields,
            )
        )
        node_repo.add_relation(
            NodeRelation(source_id="PLAN", target_id=task_id, relation_type=RelationType.CONTAINS)
        )

    add_task("T-READY", Status.READY)
    add_task(
        "T-IMPLEMENTING", Status.READY, repo="web", frontmatter={"declared_files": [shared_path]}
    )
    add_task("T-STALE", Status.IMPLEMENTING, claimed_from=Status.READY)
    add_task("T-WAITING-REVIEW", Status.IMPLEMENTED)
    add_task("T-WAITING-FIX", Status.REVIEWED, outcome=Outcome.REJECT)
    add_task("T-WAITING-MERGE", Status.REVIEWED, outcome=Outcome.APPROVE)
    add_task("T-COMPLETED", Status.COMPLETED)
    add_task("T-FAILED", Status.FAILED)
    add_task("T-DEFERRED", Status.DEFERRED)
    add_task("T-ABANDONED", Status.ABANDONED)
    add_task("T-SUPERSEDED", Status.SUPERSEDED)
    add_task("T-AWAITING-DECISION", Status.READY)
    add_task("T-BLOCKED", Status.READY)
    add_task("T-BLOCKED-BY-LEASE", Status.READY, frontmatter={"declared_files": [shared_path]})
    node_repo.save_node(
        Node(id="DECISION", kind=NodeKind.DECISION, title="Which way?", status=DecisionStatus.OPEN)
    )
    for source, target in (
        ("T-AWAITING-DECISION", "DECISION"),
        ("T-BLOCKED", "T-IMPLEMENTING"),
        ("T-BLOCKED", "T-SUPERSEDED"),
    ):
        node_repo.add_relation(
            NodeRelation(source_id=source, target_id=target, relation_type=RelationType.DEPENDS_ON)
        )
    node_repo.save_section(
        NodeSection(
            node_id="T-DEFERRED",
            section_key="deferral",
            ordinal=1,
            header="## Deferral",
            content="line one\nline two",
        )
    )
    claimed = node_repo.get_node("T-IMPLEMENTING")
    assert claimed is not None
    claimed.status, claimed.claimed_from = Status.IMPLEMENTING, Status.READY
    lease = Lease(
        task_id="T-IMPLEMENTING",
        agent_id="agent",
        session_id="session",
        branch_name="tm/T-IMPLEMENTING",
        action=Action.IMPLEMENT,
        acquired_at=datetime.now(tz=UTC),
        last_heartbeat=datetime.now(tz=UTC),
        ttl_seconds=300,
    )
    assert RuntimeRepository(db_mgr).claim(
        lease, [FileLock(file_path=shared_path, task_id="T-IMPLEMENTING")], claimed
    )
    return tmp_path


def test_statuses_counts_each_seeded_display_status_and_the_containers_roll_up(
    every_display_project: Path,
) -> None:
    client = TestClient(create_app(every_display_project))
    entries = client.get("/api/statuses").json()["statuses"]
    entry = next(e for e in entries if e["spec"] == "SPEC")
    plan = next(p for p in entry["plans"] if p["plan"] == "PLAN")
    assert plan["counts"] == {code: 1 for code in SEEDED_DISPLAY.values()}

    plan_row = client.get("/api/nodes", params={"ids": "PLAN"}).json()["items"][0]
    # Nothing re-derived the stored rollup, so the display reads the started descendant.
    assert (plan_row["status"], plan_row["display"]) == ("READY", "IMPLEMENTING")


@pytest.mark.parametrize(("task_id", "display"), sorted(SEEDED_DISPLAY.items()))
def test_every_seeded_task_reads_its_display_status(
    every_display_project: Path, task_id: str, display: str
) -> None:
    detail = TestClient(create_app(every_display_project)).get(f"/api/nodes/{task_id}").json()
    assert detail["display"] == display


def test_a_container_shows_its_stored_status_beside_its_display(
    tmp_path: Path,
) -> None:
    db_mgr = DatabaseManager(tmp_path / ".taskmanager")
    db_mgr.init_all()
    node_repo = NodeRepository(db_mgr)
    node_repo.save_node(Node(id="S", kind=NodeKind.SPEC, title="Spec", status=Status.READY))
    node_repo.save_node(Node(id="S-P1", kind=NodeKind.PLAN, title="Plan", status=Status.READY))
    node_repo.add_relation(
        NodeRelation(source_id="S", target_id="S-P1", relation_type=RelationType.CONTAINS)
    )
    node_repo.save_node(
        Node(id="S-P1-T1", kind=NodeKind.TASK, title="Task", status=Status.COMPLETED)
    )
    node_repo.add_relation(
        NodeRelation(source_id="S-P1", target_id="S-P1-T1", relation_type=RelationType.CONTAINS)
    )

    nodes = TestClient(create_app(tmp_path)).get("/api/nodes", params={"ids": "S"}).json()
    spec = nodes["items"][0]
    # Nothing re-derived the stored rollup, so the display reads the started descendant.
    assert (spec["status"], spec["display"]) == ("READY", "IMPLEMENTING")


def test_standalone_plans_and_a_specs_children_all_reach_the_root(tmp_path: Path) -> None:
    # A DB can hold specs and, separately, plans with no spec parent at all --
    # both must reach the root, not just whichever the (now-removed)
    # `if specs / else` split happened to pick.
    db_mgr = DatabaseManager(tmp_path / ".taskmanager")
    db_mgr.init_all()
    node_repo = NodeRepository(db_mgr)
    node_repo.save_node(Node(id="SPEC", kind=NodeKind.SPEC, title="Spec"))
    node_repo.save_node(Node(id="SPEC-PLAN", kind=NodeKind.PLAN, title="Spec-parented plan"))
    node_repo.save_node(Node(id="STANDALONE-PLAN", kind=NodeKind.PLAN, title="Standalone plan"))
    node_repo.save_node(Node(id="STANDALONE-TASK", kind=NodeKind.TASK, title="Standalone task"))
    node_repo.add_relation(
        NodeRelation(source_id="SPEC", target_id="SPEC-PLAN", relation_type=RelationType.CONTAINS)
    )
    node_repo.add_relation(
        NodeRelation(
            source_id="STANDALONE-PLAN",
            target_id="STANDALONE-TASK",
            relation_type=RelationType.CONTAINS,
        )
    )

    client = TestClient(create_app(tmp_path))
    root_ids = {n["id"] for n in client.get("/api/nodes").json()["items"]}
    assert "STANDALONE-PLAN" in root_ids
    assert "SPEC" in root_ids

    standalone_children = client.get("/api/nodes", params={"parent": "STANDALONE-PLAN"}).json()
    assert [c["id"] for c in standalone_children["items"]] == ["STANDALONE-TASK"]
    spec_children = client.get("/api/nodes", params={"parent": "SPEC"}).json()
    assert [c["id"] for c in spec_children["items"]] == ["SPEC-PLAN"]


def test_nodes_carry_task_score_and_dependents(every_display_project: Path) -> None:
    client = TestClient(create_app(every_display_project))
    plan_children = client.get("/api/nodes", params={"parent": "PLAN", "include": "body"}).json()[
        "items"
    ]
    tasks_by_id = {t["id"]: t for t in plan_children}

    assert isinstance(tasks_by_id["T-BLOCKED"]["score"], float)
    assert tasks_by_id["T-BLOCKED"]["body"]["dependent_details"] == []
    assert [d["id"] for d in tasks_by_id["T-IMPLEMENTING"]["body"]["dependent_details"]] == [
        "T-BLOCKED"
    ]

    plan_row = client.get("/api/nodes", params={"ids": "PLAN"}).json()["items"][0]
    assert plan_row["score"] is None


def test_a_task_lists_its_dependencies_with_their_own_status(every_display_project: Path) -> None:
    plan_children = (
        TestClient(create_app(every_display_project))
        .get("/api/nodes", params={"parent": "PLAN", "include": "body"})
        .json()["items"]
    )
    blocked = next(t for t in plan_children if t["id"] == "T-BLOCKED")

    assert blocked["display"] == "BLOCKED_BY_TASK"
    assert blocked["body"]["dependency_details"] == [
        {
            "id": "T-IMPLEMENTING",
            "title": "Task T-IMPLEMENTING",
            "kind": "task",
            "status": "IMPLEMENTING",
            "finished": False,
        },
        {
            "id": "T-SUPERSEDED",
            "title": "Task T-SUPERSEDED",
            "kind": "task",
            "status": "SUPERSEDED",
            "finished": True,
        },
    ]


def test_dependency_details_carry_kind_so_the_page_can_tell_a_decision_apart(
    tmp_path: Path,
) -> None:
    # The page shows Open/Answered/Withdrawn for a decision dependency rather than the
    # NOT_STARTED/COMPLETED/ABANDONED it reuses in storage; it needs the node's own kind to
    # tell a decision dependency apart from a task one to do that.
    db_mgr = DatabaseManager(tmp_path / ".taskmanager")
    db_mgr.init_all()
    node_repo = NodeRepository(db_mgr)
    node_repo.save_node(Node(id="T1", kind=NodeKind.TASK, title="Task"))
    node_repo.save_node(
        Node(
            id="decision-D1", kind=NodeKind.DECISION, title="Which way?", status=DecisionStatus.OPEN
        )
    )
    node_repo.add_relation(
        NodeRelation(source_id="T1", target_id="decision-D1", relation_type=RelationType.DEPENDS_ON)
    )

    detail = TestClient(create_app(tmp_path)).get("/api/nodes/T1").json()

    assert detail["dependency_details"] == [
        {
            "id": "decision-D1",
            "title": "Which way?",
            "kind": "decision",
            "status": "OPEN",
            "finished": False,
        }
    ]


def test_node_detail_lists_dependencies_and_every_section(every_display_project: Path) -> None:
    client = TestClient(create_app(every_display_project))
    blocked = client.get("/api/nodes/T-BLOCKED").json()
    deferred = client.get("/api/nodes/T-DEFERRED").json()

    assert [d["id"] for d in blocked["dependency_details"] if not d["finished"]] == [
        "T-IMPLEMENTING"
    ]
    assert deferred["display"] == "DEFERRED"
    assert deferred["sections"][0]["content"] == "line one\nline two"


def test_nodes_carry_repo_and_models_for_filtering(every_display_project: Path) -> None:
    nodes = (
        TestClient(create_app(every_display_project))
        .get("/api/nodes", params={"parent": "PLAN"})
        .json()["items"]
    )
    by_id = {n["id"]: n for n in nodes}

    assert by_id["T-COMPLETED"]["target_repo"] == "core"
    assert by_id["T-COMPLETED"]["acceptable_models"] == ["claude-opus-5"]
    assert by_id["T-IMPLEMENTING"]["target_repo"] == "web"
    assert by_id["T-IMPLEMENTING"]["acceptable_models"] == ["gemini-flash"]
    assert by_id["T-IMPLEMENTING"]["display"] == "IMPLEMENTING"


def test_static_export_embeds_every_status_and_the_filter_ui(
    every_display_project: Path, tmp_path: Path
) -> None:
    out = export_static_html(every_display_project, tmp_path / "out" / "all.html")
    html = out.read_text(encoding="utf-8")

    static_match = re.search(r"window.STATIC_DATA = (\{.*?\});</script>", html)
    assert static_match
    static = json.loads(static_match.group(1))
    entry = next(e for e in static["statuses"] if e["spec"] == "SPEC")
    plan = next(p for p in entry["plans"] if p["plan"] == "PLAN")
    assert plan["counts"] == {code: 1 for code in SEEDED_DISPLAY.values()}
    for element_id in (
        "stats-digest",
        "repo-filter",
        "model-filter",
        "spec-filter",
        "phase-filter",
        "score-filter",
        "clear-filters-btn",
        "legend-panel",
        "sidebar-resize-handle",
        "toggle-sections-btn",
    ):
        assert f'id="{element_id}"' in html
    assert html.count('id="search-box"') == 1
    assert '<aside id="sidebar-pane" class="hidden' in html
